"""
Controlled test lines for the TDR calculator: copper.txt variants of the straight
reference line (TV0), each with one section replaced, and the truth for each.

The reference is TV0: the bare board with a straight 50 ohm CPW (RO3003 0.508 mm,
0.48 mm trace, 0.12291 mm gap solved with the lid's 0.3 mm relief over it). A variant
rewrites ONLY the section between the launch pull-backs (|x| < SPLICE_X = 12.6 mm):

  * the launch lands, the SMP ground pads and the launch regions are copied byte for
    byte, and so is every line outside the section (vias with |x| >= 12.6, the bottom
    plane, the corner pads);
  * the top ground pours keep their vertices with |x| >= 12.6; between them the inner
    edge is the pour's offset of the new signal copper, at the gap;
  * the stitching lattice inside the section is regenerated from the generator's own
    rule (rows at FenceY + 0.8 k from the route, alternate rows half a step over, a site
    kept only if it keeps the via clearance from the signal and the via spacing from its
    neighbours). For the straight reference this reproduces TV0's 944 vias with |x| < 12.6
    exactly (tested).

Signal copper: straight pieces are `track` records; a mitred 90 degree corner is a pair
of `region` polygons cut by the optimal 45 degree mitre (Douville and James: the cut
starts M x W back from the outer corner, M = 0.52 + 0.65 exp(-1.35 W/h)); a curved
bend is a run of short tracks; an unmitred corner is two square-ended tracks.

Lid: the copper of a route that leaves the straight 1.2 mm relief channel lies under
the perfect-E lid, which is a short. `route_lateral_extent` says how far the signal
goes from the centre line, and make_variant_parent refuses to copy TV0's lid for it.

    python -m microwave_package.design.test_lines <TV0 copper.txt> --out <dir>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import cpw

Point = Tuple[float, float]

# ----------------------------------------------------------------- the stack
EPS_R = 3.0  # Rogers RO3003, as cpw.PERMITTIVITY lists it
H_SUB = 0.508  # mm
T_CU = 0.035  # mm, 1 oz
LID_DEPTH = 0.3  # route_clearence: the lid's ceiling over the copper
LID_CHANNEL = 1.2  # route_width: the relief channel's width (straight, over the trace)
TRACE_W = 0.48  # TV0 trace
GAP = round(cpw.solve_gap_for_z0(trace_width=TRACE_W, substrate_height=H_SUB, eps_r=EPS_R,
                                 conductor_thickness=T_CU, cover_height=LID_DEPTH,
                                 channel_width=LID_CHANNEL), 5)  # 0.12291 mm, as TV0 writes it
Z_TARGET = 50.0

# ----------------------------------------------------------------- the board
BOARD_HALF_L = 25.0
BOARD_HALF_S = 12.5
PORT_X = 13.5  # the signal land centre: the launch inner edge, port 1 at -13.5, port 2 at +13.5
SPLICE_X = 12.6  # the pull-back starts here: the inner pour edge runs straight from -12.6 to +12.6
LAUNCH_X = PORT_X

# ----------------------------------------------------------------- the stitching
VIA_DIA = 0.3
VIA_HOLE = 0.15
VIA_PITCH = 0.8  # via_max_spacing: step along a row and between rows
VIA_CLEAR = 0.15  # via_gnd_clearance: from the coplanar gap edge to the via's edge
VIA_MIN = 0.2  # via_min_spacing: edge to edge, from any other via
VIA_EDGE = 0.2  # the minimum from a via's edge to the board edge (via_min_spacing)

# ----------------------------------------------------------------- the frequencies
F_MAX_GHZ = 20.0
THRESHOLD = 0.03  # tdr.DEFAULT_THRESHOLD
EPS_EFF_DEFAULT = 2.0  # tdr.DEFAULT_EPS_EFF: the wrong number for this lid, see lid_eps_eff


def fence_y(width: float) -> float:
    """Distance of the fence's via centres from the route's centre line."""
    return width / 2.0 + GAP + VIA_CLEAR + VIA_DIA / 2.0


def fence_copper(gap: float = GAP) -> float:
    """Smallest distance from a via's centre to the signal copper: the via edge is VIA_CLEAR
    from the pour edge, which is `gap` from the copper."""
    return gap + VIA_CLEAR + VIA_DIA / 2.0


def mitre_fraction(width: float, h: float = H_SUB) -> float:
    """Douville and James's optimum mitre, as a fraction of the full mitre (cut W back from the
    outer corner): M = 0.52 + 0.65 exp(-1.35 W / h). Fitted to microstrip (W/h >= 0.25); used
    here for the grounded CPW as an approximation, and called so in the report."""
    return 0.52 + 0.65 * math.exp(-1.35 * width / h)


def mitre_depth(width: float) -> float:
    """How far back along each outer edge the mitre's cut starts, from the outer corner."""
    return mitre_fraction(width) * width


def lid_eps_eff(width: float = TRACE_W, gap: float = GAP) -> float:
    """eps_eff of the lidded line at this width and gap, from the generator's own partial
    capacitances (see cpw.z0_gcpw). It sets the TDR's distance scale: x = v t / 2."""
    a = width / 2.0
    b = a + gap
    k_sub = math.tanh(math.pi * a / (2.0 * H_SUB)) / math.tanh(math.pi * b / (2.0 * H_SUB))
    k_air = math.tanh(math.pi * a / (2.0 * LID_DEPTH)) / math.tanh(math.pi * b / (2.0 * LID_DEPTH))
    r_sub = cpw._k_ratio(k_sub)
    r_air = cpw._k_ratio(k_air) + cpw.sidewall_ratio(gap, T_CU)
    return (r_air + EPS_R * r_sub) / (r_air + r_sub)


def z0_lidded(width: float, gap: float = GAP) -> float:
    return cpw.z0_gcpw(width, gap, H_SUB, EPS_R, T_CU, cover_height=LID_DEPTH)


def gap_for_z0(width: float) -> float:
    """The gap that gives 50 ohm at this width under the lid, or the error the solver gives."""
    return cpw.solve_gap_for_z0(trace_width=width, substrate_height=H_SUB, eps_r=EPS_R,
                                conductor_thickness=T_CU, cover_height=LID_DEPTH,
                                channel_width=LID_CHANNEL)


# ================================================================ geometry

def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = float(np.hypot(v[0], v[1]))
    if n == 0.0:
        raise ValueError("zero-length segment")
    return v / n


def _left(u) -> np.ndarray:
    return np.array([-u[1], u[0]])


def _cross(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _intersect(p1, d1, p2, d2) -> Optional[np.ndarray]:
    denom = _cross(d1, d2)
    if abs(denom) < 1e-12:
        return None
    t = _cross(np.asarray(p2) - np.asarray(p1), d2) / denom
    return np.asarray(p1) + t * np.asarray(d1)


def offset_polyline(vertices, distances, side: int) -> np.ndarray:
    """Mitred offset of an open polyline. Segment j is pushed `distances[j]` to the left
    (side = +1) or right (side = -1) of its direction; each interior vertex takes the
    intersection of its two offset lines. A (nearly) straight vertex with a step in the
    distance takes the offset of the earlier segment, so the step is a square step."""
    v = np.asarray(vertices, dtype=float)
    n = len(v) - 1
    if n < 1 or len(distances) != n:
        raise ValueError("need one distance per segment")
    u = [_unit(v[j + 1] - v[j]) for j in range(n)]
    out = [v[0] + side * distances[0] * _left(u[0])]
    for m in range(1, n):
        q0 = v[m] + side * distances[m - 1] * _left(u[m - 1])
        q1 = v[m] + side * distances[m] * _left(u[m])
        x = _intersect(q0, u[m - 1], q1, u[m])
        if x is None:
            x = q0
        out.append(x)
    out.append(v[-1] + side * distances[-1] * _left(u[-1]))
    return np.array(out)


def _clip_halfplane(poly: Sequence[Point], normal, c: float) -> List[Point]:
    """Sutherland-Hodgman: the part of a polygon where p . normal <= c."""
    out: List[Point] = []
    n = len(poly)
    nx, ny = float(normal[0]), float(normal[1])
    for i in range(n):
        p = np.asarray(poly[i], dtype=float)
        q = np.asarray(poly[(i + 1) % n], dtype=float)
        fp = p[0] * nx + p[1] * ny - c
        fq = q[0] * nx + q[1] * ny - c
        if fp <= 0:
            out.append((float(p[0]), float(p[1])))
        if (fp < 0 < fq) or (fq < 0 < fp):
            t = fp / (fp - fq)
            r = p + t * (q - p)
            out.append((float(r[0]), float(r[1])))
    return out


def polygon_is_simple(points: Sequence[Point]) -> bool:
    """No two non-adjacent edges of the closed polygon cross or touch."""
    pts = np.asarray(points, dtype=float)
    n = len(pts)
    if n < 3:
        return False

    def seg_cross(a, b, c, d) -> bool:
        def orient(p, q, r):
            return _cross(q - p, r - p)
        d1, d2 = orient(c, d, a), orient(c, d, b)
        d3, d4 = orient(a, b, c), orient(a, b, d)
        return (d1 * d2 < -1e-15) and (d3 * d4 < -1e-15)

    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if seg_cross(a, b, c, d):
                return False
    return True


# ================================================================ routes

@dataclass(frozen=True)
class Route:
    """A centre line from port 1 to port 2, with a width for each segment.

    `joints[m-1]` describes the vertex m (interior): 'straight' (collinear, or a width step),
    'arc' (a sample of a curved bend), 'sharp' (an unmitred 90 degree corner) or 'mitre'
    (a 90 degree corner cut by the optimal 45 degree mitre)."""
    name: str
    vertices: Tuple[Point, ...]
    widths: Tuple[float, ...]
    joints: Tuple[str, ...]
    marks: Tuple[Tuple[float, str], ...] = ()  # (arc length, label) of each corner or bend
    groups: Tuple[Tuple[str, str, float, float], ...] = ()  # (label, matched/unmatched/step, s from, s to)

    def __post_init__(self):
        if len(self.widths) != len(self.vertices) - 1:
            raise ValueError("one width per segment")
        if len(self.joints) != len(self.vertices) - 2:
            raise ValueError("one joint per interior vertex")

    @property
    def length(self) -> float:
        v = np.asarray(self.vertices)
        return float(np.sum(np.hypot(*(v[1:] - v[:-1]).T)))

    def arc_at_vertices(self) -> np.ndarray:
        v = np.asarray(self.vertices)
        return np.concatenate([[0.0], np.cumsum(np.hypot(*(v[1:] - v[:-1]).T))])

    def lateral_extent(self) -> float:
        """Largest |y| the signal copper reaches, half widths included."""
        v = np.asarray(self.vertices)
        h = np.asarray(self.widths) / 2.0
        return float(max(np.max(np.abs(v[:, 1])), 0.0) + np.max(h))

    def footprint_extent(self) -> float:
        """Largest distance from the centre line to the edge of the CPW footprint: the trace, its
        half width, and the gap to the pour. The lid's channel must clear this, not the copper."""
        return self.lateral_extent() + GAP

    def x_at_arc(self, s: float) -> float:
        return float(self.point_at(s)[0])

    def point_at(self, s: float) -> np.ndarray:
        arc = self.arc_at_vertices()
        v = np.asarray(self.vertices)
        i = int(np.clip(np.searchsorted(arc, s, side="right") - 1, 0, len(v) - 2))
        span = arc[i + 1] - arc[i]
        t = 0.0 if span == 0 else (s - arc[i]) / span
        return v[i] + t * (v[i + 1] - v[i])


def _arc_samples(p_prev, p, p_next, radius: float, sample_deg: float):
    u_in = _unit(np.asarray(p, float) - np.asarray(p_prev, float))
    u_out = _unit(np.asarray(p_next, float) - np.asarray(p, float))
    cross = _cross(u_in, u_out)
    dot = float(np.dot(u_in, u_out))
    theta = math.atan2(abs(cross), dot)
    if theta <= 1e-9:
        raise ValueError("an arc needs a corner")
    t = radius * math.tan(theta / 2.0)
    t_in = np.asarray(p, float) - t * u_in
    t_out = np.asarray(p, float) + t * u_out
    turn = 1.0 if cross > 0 else -1.0
    centre = t_in + turn * radius * _left(u_in)
    a0 = math.atan2(*(t_in - centre)[::-1])
    n = max(2, int(math.ceil(math.degrees(theta) / sample_deg)))
    samples = []
    for k in range(1, n):
        a = a0 + turn * theta * k / n
        samples.append(centre + radius * np.array([math.cos(a), math.sin(a)]))
    return t_in, samples, t_out, theta


def build_route(name: str, corners: Sequence, leg_widths: Sequence[float],
                sample_deg: float = 2.0) -> Route:
    """A route from corners.

    `corners`: (x, y, kind) for each corner, first and last 'end'. kind is 'straight',
    'sharp', 'mitre' or ('arc', R): an arc of radius R replaces the corner, tangent to both
    legs. `leg_widths[i]` is the width of the leg from corner i to corner i+1. Two arcs with no
    straight between them (an S-bend) share a tangent point: it is written once."""
    pts = [np.asarray(c[:2], dtype=float) for c in corners]
    kinds = [c[2] for c in corners]
    if kinds[0] != "end" or kinds[-1] != "end":
        raise ValueError("the first and last corners are the ports ('end')")
    if len(leg_widths) != len(corners) - 1:
        raise ValueError("one width per leg")
    items: List[Tuple[np.ndarray, Optional[str]]] = [(pts[0], None)]
    widths: List[float] = []
    marks_at: List[Tuple[int, str]] = []  # (item index, label) of each corner

    def add(point: np.ndarray, joint: Optional[str], width: float) -> int:
        if math.hypot(*(point - items[-1][0])) < 1e-9:
            return len(items) - 1  # a shared tangent point: already written
        items.append((point, joint))
        widths.append(width)
        return len(items) - 1

    for i in range(len(pts) - 1):
        cur = float(leg_widths[i])
        c = i + 1
        if c == len(pts) - 1:
            add(pts[c], None, cur)
            break
        kind = kinds[c]
        if isinstance(kind, tuple) and kind[0] == "arc":
            t_in, samples, t_out, _ = _arc_samples(pts[c - 1], pts[c], pts[c + 1], float(kind[1]),
                                                   sample_deg)
            add(t_in, "straight", cur)
            first_sample = len(items)
            for s in samples:
                add(s, "arc", cur)
            marks_at.append((first_sample + (len(samples) - 1) // 2 if samples else len(items) - 1,
                             f"bend R={float(kind[1]):g}"))
            add(t_out, "straight", cur)
        elif kind in ("straight", "sharp", "mitre"):
            k = add(pts[c], kind, cur)
            if kind != "straight":
                marks_at.append((k, f"{kind} 90 deg"))
        else:
            raise ValueError(f"unknown corner kind {kind!r}")
    vertices = tuple((float(p[0]), float(p[1])) for p, _ in items)
    joints = tuple(j for _, j in items[1:-1])
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(*(np.diff(np.asarray(vertices), axis=0)).T))])
    marks = tuple((float(arc[k]), label) for k, label in marks_at)
    return Route(name, vertices, tuple(widths), joints, marks)


def _outer_side(route: Route, m: int) -> int:
    """The side (+1 left, -1 right) on which the joint at vertex m is the outer corner."""
    v = np.asarray(route.vertices)
    u0 = _unit(v[m] - v[m - 1])
    u1 = _unit(v[m + 1] - v[m])
    cross = _cross(u0, u1)
    return -1 if cross > 0 else 1


def _segment_frames(route: Route):
    v = np.asarray(route.vertices, dtype=float)
    u = [_unit(v[j + 1] - v[j]) for j in range(len(v) - 1)]
    h = np.asarray(route.widths, dtype=float) / 2.0
    return v, u, h


def _mitre_points(route: Route, m: int, side: int):
    """(O, A, B, N_cut) of the mitre at vertex m, for the outer side `side`."""
    v, u, h = _segment_frames(route)
    j0, j1 = m - 1, m
    n_out0 = side * _left(u[j0])
    n_out1 = side * _left(u[j1])
    if abs(float(np.dot(u[j0], u[j1]))) > 1e-9:
        raise ValueError("a mitre needs a 90 degree corner")
    if abs(h[j0] - h[j1]) > 1e-12:
        raise ValueError("a mitre needs the same width on both legs")
    w = 2.0 * h[j0]
    d = mitre_depth(w)
    o = v[m] + h[j0] * (n_out0 + n_out1)
    a = o - d * u[j0]
    b = o + d * u[j1]
    n_cut = _unit(o - v[m])
    return o, a, b, n_cut, n_out0, n_out1


def side_boundary(route: Route, side: int) -> np.ndarray:
    """The copper's edge on one side (+1 left, -1 right), from port 1 to port 2. Mitred corners
    on the outer side have their chamfer; everything else is the mitred join of the edges."""
    v, u, h = _segment_frames(route)
    nseg = len(u)
    out = [v[0] + side * h[0] * _left(u[0])]
    for m in range(1, nseg):
        j0, j1 = m - 1, m
        joint = route.joints[m - 1]
        e0 = v[m] + side * h[j0] * _left(u[j0])
        e1 = v[m] + side * h[j1] * _left(u[j1])
        if joint == "straight":
            if abs(h[j0] - h[j1]) < 1e-12:
                out.append(v[m] + side * h[j0] * _left(u[j0]))
            else:
                out.append(e0)
                out.append(e1)
        elif joint == "mitre" and side == _outer_side(route, m):
            _, a, b, _, _, _ = _mitre_points(route, m, side)
            out.append(a)
            out.append(b)
        else:
            x = _intersect(e0, u[j0], e1, u[j1])
            out.append(e0 if x is None else x)
    out.append(v[-1] + side * h[-1] * _left(u[-1]))
    return np.array(out)


def signal_pieces(route: Route) -> List[dict]:
    """The copper of the signal, as pieces.

    Straight pieces are `track` records whose ends are the centre-line vertices (an arc joint
    butts its neighbours together, leaving an 8 micron notch on the outer side for 2 degree
    samples). A 90 degree 'sharp' corner is its two legs, ending on the vertex, plus a `region`
    square of side W on the vertex, which fills the outer corner. A 'mitre' corner is two
    `region` polygons, each leg cut by the optimal 45 degree mitre (and by the outer edge of the
    other leg), which share the cut exactly."""
    v, u, h = _segment_frames(route)
    nseg = len(u)
    pieces: List[dict] = []
    for j in range(nseg):
        hw = h[j]
        clips: List[Tuple[np.ndarray, float]] = []  # (normal, c) keep p.normal <= c
        for which, m in (("start", j), ("end", j + 1)):
            if m == 0 or m == nseg:
                continue
            joint = route.joints[m - 1]
            if joint == "mitre":
                side = _outer_side(route, m)
                o, a, b, n_cut, n_out0, n_out1 = _mitre_points(route, m, side)
                clips.append((n_cut, float(np.dot(n_cut, a))))
                # stop short of the other leg's outer edge: a leg that starts at V reaches back
                # past V into the leg before it, and a leg that ends at V reaches forward into the next
                other = n_out0 if which == "start" else n_out1
                clips.append((other, float(np.dot(other, v[m] + hw * other))))
        rect = [tuple(v[j] + hw * _left(u[j])), tuple(v[j + 1] + hw * _left(u[j])),
                tuple(v[j + 1] - hw * _left(u[j])), tuple(v[j] - hw * _left(u[j]))]
        if not clips:
            pieces.append({"kind": "track", "a": (float(v[j][0]), float(v[j][1])),
                           "b": (float(v[j + 1][0]), float(v[j + 1][1])), "width": float(2 * hw)})
            continue
        poly: Sequence[Point] = rect
        for normal, c in clips:
            poly = _clip_halfplane(poly, normal, c)
        if len(poly) < 3:
            raise ValueError(f"segment {j} clipped away")
        pieces.append({"kind": "region", "points": [(round(x, 9), round(y, 9)) for x, y in poly]})
    for m in range(1, nseg):
        if route.joints[m - 1] != "sharp":
            continue
        hw = h[m]
        n0, n1 = _left(u[m - 1]), _left(u[m])
        centre = v[m]
        square = [centre + hw * (n0 + n1), centre + hw * (-n0 + n1),
                  centre - hw * (n0 + n1), centre + hw * (n0 - n1)]
        pieces.append({"kind": "region", "points": [(round(float(p[0]), 9), round(float(p[1]), 9))
                                                    for p in square]})
    return pieces


def piece_polygons(pieces: Sequence[dict]) -> List[np.ndarray]:
    """Each piece as a closed polygon (rectangle for a track)."""
    polys = []
    for p in pieces:
        if p["kind"] == "track":
            a, b = np.asarray(p["a"]), np.asarray(p["b"])
            u = _unit(b - a)
            n = _left(u) * p["width"] / 2.0
            polys.append(np.array([a + n, b + n, b - n, a - n]))
        else:
            polys.append(np.asarray(p["points"], dtype=float))
    return polys


def copper_distance(points: np.ndarray, polys: Sequence[np.ndarray]) -> np.ndarray:
    """Distance from each point to the nearest copper (0 inside a polygon)."""
    pts = np.atleast_2d(np.asarray(points, dtype=float))
    best = np.full(len(pts), np.inf)
    inside = np.zeros(len(pts), dtype=bool)
    for poly in polys:
        a = poly
        b = np.roll(poly, -1, axis=0)
        ab = b - a
        denom = np.sum(ab * ab, axis=1)
        for k in range(len(a)):
            ap = pts - a[k]
            t = np.clip(np.sum(ap * ab[k], axis=1) / max(denom[k], 1e-300), 0.0, 1.0)
            d = np.hypot(*(pts - (a[k] + t[:, None] * ab[k])).T)
            best = np.minimum(best, d)
        # even-odd test for inside
        x, y = pts[:, 0], pts[:, 1]
        crossing = np.zeros(len(pts), dtype=bool)
        for k in range(len(a)):
            x1, y1 = a[k]
            x2, y2 = b[k]
            cond = (y1 > y) != (y2 > y)
            with np.errstate(divide="ignore", invalid="ignore"):
                xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            crossing ^= cond & (x < xint)
        inside |= crossing
    best[inside] = 0.0
    return best


# ================================================================ copper.txt

_TRACK_LINE = "track|top|-13.5|0|13.5|0|0.48"


def _fmt(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _parse_point_list(text: str) -> List[Point]:
    nums = [float(v) for v in text.split(",") if v != ""]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]


@dataclass
class VariantReport:
    name: str
    vias_removed: int
    vias_placed: int
    vias_refused: int
    pour_splices: int
    signal_tracks: int
    signal_regions: int


def regenerate_vias(route: Route, pieces: Sequence[dict], kept: np.ndarray) -> Tuple[np.ndarray, Dict[str, object]]:
    """Stitching sites inside the section, by the generator's own rules.

    Candidates, in priority order:
      1. the FENCE: row 0 on both sides of the route, at FenceY(local width) from the centre line,
         every 0.8 mm along the centre line from the point where the route crosses x = 0. A
         width step moves the fence; it does not move the lattice.
      2. the outer bisector at each mitred or sharp corner, at the fence distance: the rows leave a
         gap of more than one pitch there otherwise.
      3. the FILL: the TV0 lattice itself, rows |y| = FenceY(TRACE_W) + 0.8 j for j >= 1, with
         even rows on multiples of 0.8 and odd rows half a step over, as TV0 has them. The fill is
         the generator's global lattice, not the route's offsets, so it stays regular away from the
         route and only loses sites where the route's copper or the fence is.

    A candidate is kept if it is at least fence_copper from the signal, at least VIA_DIA + VIA_MIN
    from every site kept before it and from the kept vias outside the section, and inside the
    board's via limits. Returns (sites, counts), with counts['copper'], counts['spacing'] and the
    refused positions under counts['log']."""
    polys = piece_polygons(pieces)
    v = np.asarray(route.vertices, dtype=float)
    widths = np.asarray(route.widths, dtype=float)
    arc = route.arc_at_vertices()
    total = float(arc[-1])
    u = [_unit(v[j + 1] - v[j]) for j in range(len(v) - 1)]
    fence_limit = fence_copper()
    board_s = BOARD_HALF_S - VIA_EDGE - VIA_DIA / 2.0
    board_l = BOARD_HALF_L - VIA_EDGE - VIA_DIA / 2.0
    s_anchor = _crossing_arc(v, arc)
    fixed = np.asarray(kept, dtype=float).reshape(-1, 2)
    fixed = fixed[np.abs(fixed[:, 0]) >= SPLICE_X - 1e-9] if len(fixed) else fixed

    candidates: List[np.ndarray] = []
    # 1. the fence, both sides, every 0.8 mm along the centre line from the x = 0 crossing
    fence_d = fence_y_array(widths)
    m_lo = int(math.floor((0.0 - s_anchor) / VIA_PITCH)) - 1
    m_hi = int(math.ceil((total - s_anchor) / VIA_PITCH)) + 1
    for side in (1, -1):
        for m in range(m_lo, m_hi + 1):
            s = s_anchor + VIA_PITCH * m
            if s < -1e-12 or s > total + 1e-12:
                continue
            j = int(np.clip(np.searchsorted(arc, s, side="right") - 1, 0, len(u) - 1))
            span = arc[j + 1] - arc[j]
            t = 0.0 if span == 0 else (s - arc[j]) / span
            candidates.append(v[j] + t * (v[j + 1] - v[j]) + side * fence_d[j] * _left(u[j]))
    # 2. the outer bisector at each mitred or sharp corner, at the fence distance
    for m in range(1, len(v) - 1):
        if route.joints[m - 1] not in ("mitre", "sharp"):
            continue
        bisector = _unit(_left(u[m - 1]) + _left(u[m]))
        candidates.append(v[m] + _outer_side(route, m) * bisector * fence_y(route.widths[m - 1]) * math.sqrt(2.0))
    # 3. the fill: the TV0 lattice, rows j = 1 ... (the fence row is j = 0)
    base = fence_y(TRACE_W)
    for j_row in range(1, 40):
        y_row = base + VIA_PITCH * j_row
        if y_row > board_s + 1e-9:
            break
        x0 = 0.0 if j_row % 2 == 0 else 0.5 * VIA_PITCH
        m_x_lo = int(math.floor((-SPLICE_X - x0) / VIA_PITCH)) - 1
        m_x_hi = int(math.ceil((SPLICE_X - x0) / VIA_PITCH)) + 1
        for sign in (1, -1):
            for mx in range(m_x_lo, m_x_hi + 1):
                candidates.append(np.array([x0 + VIA_PITCH * mx, sign * y_row]))

    cand = np.asarray(candidates, dtype=float).reshape(-1, 2)
    inside_section = (np.abs(cand[:, 0]) < SPLICE_X - 1e-9) & (np.abs(cand[:, 1]) <= board_s + 1e-9)         & (np.abs(cand[:, 0]) <= board_l + 1e-9)
    clearance = np.full(len(cand), np.inf)
    clearance[inside_section] = copper_distance(cand[inside_section], polys)

    accepted = np.zeros((len(cand) + len(fixed), 2))
    count = 0
    if len(fixed):
        accepted[:len(fixed)] = fixed
        count = len(fixed)
    fixed_count = count
    kept_sites: List[Tuple[float, float]] = []
    refused = {"copper": 0, "spacing": 0}
    log: List[Tuple[float, float, str]] = []
    for idx in range(len(cand)):
        p = cand[idx]
        if not inside_section[idx]:
            continue
        if clearance[idx] < fence_limit - 1e-7:
            refused["copper"] += 1
            log.append((float(p[0]), float(p[1]), "copper"))
            continue
        if count and float(np.min(np.hypot(*(accepted[:count] - p).T))) < VIA_DIA + VIA_MIN - 1e-7:
            refused["spacing"] += 1
            log.append((float(p[0]), float(p[1]), "spacing"))
            continue
        accepted[count] = p
        count += 1
        kept_sites.append((float(p[0]), float(p[1])))
    sites_out = np.asarray(kept_sites, dtype=float).reshape(-1, 2)
    refused["log"] = log
    return sites_out, refused


def fence_y_array(widths: np.ndarray) -> np.ndarray:
    return np.array([fence_y(w) for w in widths])


def _crossing_arc(row: np.ndarray, arc: np.ndarray) -> float:
    """Arc length along the row where x = 0 (first crossing); 0 if it never crosses."""
    for i in range(len(row) - 1):
        x0, x1 = row[i, 0], row[i + 1, 0]
        if (x0 <= 0.0 <= x1) or (x1 <= 0.0 <= x0):
            if x1 == x0:
                return float(arc[i])
            return float(arc[i] + (0.0 - x0) / (x1 - x0) * (arc[i + 1] - arc[i]))
    return 0.0


def _point_on(row: np.ndarray, arc: np.ndarray, s: float) -> Tuple[float, float]:
    i = int(np.clip(np.searchsorted(arc, s, side="right") - 1, 0, len(row) - 2))
    span = arc[i + 1] - arc[i]
    t = 0.0 if span == 0 else (s - arc[i]) / span
    p = row[i] + t * (row[i + 1] - row[i])
    return float(p[0]), float(p[1])


def _clip_polyline_x(points: np.ndarray, x0: float, x1: float) -> np.ndarray:
    """The part of an x-monotone-or-not polyline between x0 and x1 (walked in order), with the
    end points placed exactly at x0 and x1 where the line enters or leaves that band."""
    out: List[np.ndarray] = []
    inside_prev = None
    for i in range(len(points) - 1):
        p, q = points[i], points[i + 1]
        if i == 0:
            inside_prev = x0 <= p[0] <= x1
            if inside_prev:
                out.append(p)
        for bound in (x0, x1):
            if (p[0] - bound) * (q[0] - bound) < 0:
                t = (bound - p[0]) / (q[0] - p[0])
                r = p + t * (q - p)
                if x0 <= r[0] <= x1:
                    out.append(r)
        inside_q = x0 <= q[0] <= x1
        if inside_q:
            out.append(q)
        inside_prev = inside_q
    return np.asarray(out)


def pour_inner_edge(route: Route, side: int) -> np.ndarray:
    """The pour's inner edge: the copper's side boundary offset by the gap, outward, clipped to
    the section |x| <= SPLICE_X. The end points are on x = +-SPLICE_X at the pour's straight
    edge."""
    edge = side_boundary(route, side)
    dists = [GAP] * (len(edge) - 1)
    pour = offset_polyline(edge, dists, side)
    return _clip_polyline_x(pour, -SPLICE_X, SPLICE_X)


def splice_region(points: List[Point], inner_path: np.ndarray, ) -> List[Point]:
    """Replace the straight inner edge (-SPLICE_X, y0)..(SPLICE_X, y0) of a TV0 pour polygon with
    the offset path, keeping every vertex outside it exactly."""
    y0 = inner_path[0, 1]
    idx_l = idx_r = None
    for i, (x, y) in enumerate(points):
        if abs(x + SPLICE_X) < 1e-9 and abs(y - y0) < 1e-9 and idx_l is None:
            idx_l = i
        if abs(x - SPLICE_X) < 1e-9 and abs(y - y0) < 1e-9 and idx_l is not None and idx_r is None:
            idx_r = i
    if idx_l is None or idx_r is None or idx_r != idx_l + 1:
        raise ValueError("the pour's straight inner edge is not where TV0 has it")
    interior = [(float(x), float(y)) for x, y in inner_path[1:-1]]
    return points[:idx_l + 1] + interior + points[idx_r:]


def _is_section_via(line: str) -> bool:
    return line.startswith("via|") and abs(float(line.split("|")[1])) < SPLICE_X - 1e-9


def section_free_lines(text: str) -> List[str]:
    """The copper.txt lines outside the section: everything except the signal track and regions
    (inside the launch edges), the vias with |x| < SPLICE_X, and the top pours that the splice changes. A variant and its parent must
    agree on these, in order, or the parent is not the reference for the variant."""
    out = []
    for line in text.splitlines():
        if line.startswith("track|") or _is_section_via(line):
            continue
        if line.startswith("region|top|"):
            pts = _parse_point_list(line.split("|", 2)[2])
            if len(pts) >= 82 and any(abs(abs(x) - SPLICE_X) < 1e-9 for x, _ in pts):
                continue  # a top pour the splice changes
            if all(abs(x) <= PORT_X + 1e-9 for x, _ in pts):
                continue  # a signal region (mitred leg, corner square): inside the launch edges
        out.append(line)
    return out


def write_variant(base_text: str, route: Route, out_path: Optional[str] = None) -> Tuple[str, VariantReport]:
    """The variant's copper.txt, built from TV0's text. Returns (text, report)."""
    lines = base_text.splitlines()
    pieces = signal_pieces(route)
    tracks_out: List[str] = []
    regions_out: List[str] = []
    for p in pieces:
        if p["kind"] == "track":
            a, b = p["a"], p["b"]
            tracks_out.append(f"track|top|{_fmt(a[0])}|{_fmt(a[1])}|{_fmt(b[0])}|{_fmt(b[1])}|{_fmt(p['width'])}")
        else:
            coords = ",".join(_fmt(c) for pt in p["points"] for c in pt)
            regions_out.append(f"region|top|{coords}")

    # The pours: the two 82-vertex top regions (y > 0 and y < 0) get the spliced inner edges.
    left = pour_inner_edge(route, +1)
    right = pour_inner_edge(route, -1)
    splices = 0
    new_lines: List[str] = []
    vias_removed = 0
    kept_vias: List[Tuple[float, float]] = []
    via_block_index = None
    track_seen = 0
    for line in lines:
        if line == _TRACK_LINE:
            track_seen += 1
            new_lines.extend(tracks_out)
            new_lines.extend(regions_out)
            continue
        if line.startswith("track|"):
            raise ValueError(f"unexpected track in TV0: {line!r}")
        if line.startswith("region|top|"):
            points = _parse_point_list(line.split("|", 2)[2])
            if len(points) == 82 and any(abs(x + SPLICE_X) < 1e-9 for x, _ in points):
                # the y > 0 pour lies on the left of the route (direction +x), the y < 0 pour on the right
                if max(y for _, y in points) > 0:
                    points = splice_region(points, left)
                else:
                    points = splice_region(points, right)
                splices += 1
                coords = ",".join(_fmt(c) for pt in points for c in pt)
                new_lines.append(f"region|top|{coords}")
                continue
            new_lines.append(line)
            continue
        if line.startswith("via|"):
            _, xs, ys, _, _ = line.split("|")
            x, y = float(xs), float(ys)
            if abs(x) < SPLICE_X - 1e-9:
                vias_removed += 1
                if via_block_index is None:
                    via_block_index = len(new_lines)
                    new_lines.append("__VIAS__")
                continue
            kept_vias.append((x, y))
            new_lines.append(line)
            continue
        new_lines.append(line)
    if track_seen != 1:
        raise ValueError("TV0 must carry exactly one straight track")
    if splices != 2:
        raise ValueError("TV0 must carry two 82-vertex top pours")

    sites, counts = regenerate_vias(route, pieces, np.asarray(kept_vias))
    refused = int(counts["copper"]) + int(counts["spacing"])
    via_lines = [f"via|{_fmt(x)}|{_fmt(y)}|{_fmt(VIA_DIA)}|{_fmt(VIA_HOLE)}" for x, y in sites]
    final: List[str] = []
    for line in new_lines:
        if line == "__VIAS__":
            final.extend(via_lines)
        else:
            final.append(line)
    text = "\n".join(final) + "\n"
    report = VariantReport(name=route.name, vias_removed=vias_removed, vias_placed=len(sites),
                           vias_refused=refused, pour_splices=splices,
                           signal_tracks=len(tracks_out), signal_regions=len(regions_out))
    if out_path is not None:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return text, report


# ================================================================ the variants

def tv1_step() -> Route:
    """TV1: a sharp step to a 0.30 mm trace over 8 mm at the same gap (0.12291 mm)."""
    corners = [(-PORT_X, 0.0, "end"), (-4.0, 0.0, "straight"), (4.0, 0.0, "straight"),
               (PORT_X, 0.0, "end")]
    return build_route("TV1", corners, [TRACE_W, 0.30, TRACE_W])


def tv2_taper() -> Route:
    """TV2: a smooth taper, 0.48 -> 0.30 mm and back, raised-cosine over 12 mm (3 resolutions),
    sampled every 0.25 mm; the gap stays 0.12291 mm throughout."""
    xs = [-PORT_X] + [-6.0 + 0.25 * i for i in range(49)] + [PORT_X]
    corners = [(xs[0], 0.0, "end")] + [(x, 0.0, "straight") for x in xs[1:-1]] + [(xs[-1], 0.0, "end")]
    widths = []
    for a, b in zip(xs[:-1], xs[1:]):
        mid = 0.5 * (a + b)
        if abs(mid) <= 6.0:
            w = TRACE_W - 0.18 * (1.0 + math.cos(math.pi * mid / 6.0)) / 2.0  # 0.30 at 0, 0.48 at |x| = 6
        else:
            w = TRACE_W
        widths.append(w)
    return build_route("TV2", corners, widths)


# ----------------------------------------------------------------- the routed family (TV3..TV5)
# The 12 mm pocket is not buildable (the SMP window check). The routed family lives in the
# 2.7 mm relief channel (route_width 2.7, route_clearence 0.3): the copper edge must stay
# POCKET_MARGIN inside the wall, which leaves a centre line within about +-0.9 mm of the axis.
# Every corner is a stack or a bump: a route that starts and ends on the axis has an even
# number of 90 degree corners, so a stack of three cannot close; a bump of four corners can.
POCKET_WIDTH = 2.7
POCKET_HALF = POCKET_WIDTH / 2.0
POCKET_MARGIN = 0.15  # the CPW footprint (trace and both gaps) stays this far inside the lid's wall
BUMP_H = 0.8  # a stack's bump height on the centre line
BUMP_W = 0.8  # a stack's top width: four corners within 2.4 mm
WAVE_PLATEAU = 0.05  # the straight between the two S-bends of a wave
WAVE_LATERAL = 0.80  # the wave's lateral offset on the centre line: footprint margin 0.19 mm
WAVE_MATCHED = (1.44, math.acos(1.0 - WAVE_LATERAL / (2.0 * 1.44)))  # R = 3 W: 43.8 degrees
WAVE_TIGHT = (0.48, math.acos(1.0 - WAVE_LATERAL / (2.0 * 0.48)))  # R = W: 80.4 degrees
FEATURE_X = 9.6  # the feature zone: one resolution (3.9 mm) clear of each launch
RESOLUTION_MM = 3.89  # the 20 GHz distance resolution of the lidded line (report)
STEP_X = (-4.0, 4.0)  # TV5's 0.30 mm section, 8 mm long


def _bump(x0: float, kind: str) -> List[Tuple[float, float, object]]:
    """Four corners of a bump: up, along, down, along, all within BUMP_W x BUMP_H."""
    return [(x0, 0.0, kind), (x0, BUMP_H, kind), (x0 + BUMP_W, BUMP_H, kind), (x0 + BUMP_W, 0.0, kind)]


def _wave(x0: float, radius: float, theta: float):
    """Two S-bends of radius R and angle theta: up to lateral d and back, with a short plateau.
    Four curved corners. Returns (corners, x of the wave's end); the wave starts at x0."""
    t = radius * math.tan(theta / 2.0)  # tangent length of each arc
    d = 2.0 * radius * (1.0 - math.cos(theta))  # lateral offset of one S-bend
    run = d / math.tan(theta)
    a1 = x0 + t
    b1 = a1 + run
    a2 = b1 + 2.0 * t + WAVE_PLATEAU
    b2 = a2 + run
    arc = ("arc", radius)
    return [(a1, 0.0, arc), (b1, d, arc), (a2, d, arc), (b2, 0.0, arc)], b2 + t


def _wave_extent(radius: float, theta: float) -> float:
    return _wave(0.0, radius, theta)[1]


def wave_lateral(radius: float, theta: float) -> float:
    return 2.0 * radius * (1.0 - math.cos(theta))


def _routed_line(name: str, middle: Sequence, step: Optional[Tuple[float, float, float]] = None) -> Route:
    corners = [(-PORT_X, 0.0, "end")] + list(middle) + [(PORT_X, 0.0, "end")]
    widths = []
    for (ax, ay, _), (bx, by, _) in zip(corners[:-1], corners[1:]):
        mx, my = 0.5 * (ax + bx), 0.5 * (ay + by)
        inside = step is not None and abs(my) < 1e-9 and step[0] - 1e-9 <= mx <= step[1] + 1e-9
        widths.append(step[2] if inside else TRACE_W)
    return build_route(name, corners, widths)


def _spans(route: Route, boxes) -> Tuple[Tuple[str, str, float, float], ...]:
    """Arc range of each group, from the vertices inside its box (x0, x1, y1), y from 0 to y1."""
    v = np.asarray(route.vertices)
    arc = route.arc_at_vertices()
    out = []
    for label, status, x0, x1, y1 in boxes:
        sel = (v[:, 0] >= x0 - 1e-9) & (v[:, 0] <= x1 + 1e-9) & (v[:, 1] >= -1e-9) & (v[:, 1] <= y1 + 1e-9)
        out.append((label, status, float(arc[sel].min()), float(arc[sel].max())))
    return tuple(out)


def tv3_matched() -> Route:
    """TV3, matched only: a stack of four optimal 45 degree mitres (a bump), then a matched wave of
    two S-bends of radius 1.44 mm (3 W), separated by two resolutions."""
    bump = _bump(-FEATURE_X, "mitre")
    ext = _wave_extent(*WAVE_MATCHED)
    wx0 = FEATURE_X - ext
    wave, _ = _wave(wx0, *WAVE_MATCHED)
    route = _routed_line("TV3", bump + wave)
    return replace(route, groups=_spans(route, [
        ("stack M1: four optimal mitres (matched corners)", "matched", -FEATURE_X, -FEATURE_X + BUMP_W, BUMP_H),
        ("wave W1: two S-bends, R 1.44 mm (matched bends)", "matched", wx0, FEATURE_X, wave_lateral(*WAVE_MATCHED)),
    ]))


def tv4_unmatched() -> Route:
    """TV4, unmatched only: two stacks of four unmitred 90 degree corners (bumps), with a tight wave
    of two S-bends of radius 0.48 mm (1 W) between them, each separated by two resolutions."""
    bump1 = _bump(-FEATURE_X, "sharp")
    bump2 = _bump(FEATURE_X - BUMP_W, "sharp")
    ext = _wave_extent(*WAVE_TIGHT)
    wx0 = -0.5 * ext
    wave, _ = _wave(wx0, *WAVE_TIGHT)
    route = _routed_line("TV4", bump1 + wave + bump2)
    return replace(route, groups=_spans(route, [
        ("stack U1: four unmitred corners (unmatched corners)", "unmatched", -FEATURE_X, -FEATURE_X + BUMP_W, BUMP_H),
        ("wave W2: two S-bends, R 0.48 mm (unmatched bends)", "unmatched-bend", wx0, -wx0, wave_lateral(*WAVE_TIGHT)),
        ("stack U2: four unmitred corners (unmatched corners)", "unmatched", FEATURE_X - BUMP_W, FEATURE_X, BUMP_H),
    ]))


def tv5_combined() -> Route:
    """TV5: a stack of four optimal mitres (matched), a 0.30 mm section 8 mm long at the centre, and a
    stack of four unmitred corners (unmatched). The stacks are one resolution from the section: the
    27 mm launch to launch leaves no room for two resolutions on each side."""
    bump1 = _bump(-FEATURE_X, "mitre")
    bump2 = _bump(FEATURE_X - BUMP_W, "sharp")
    middle = bump1 + [(STEP_X[0], 0.0, "straight"), (STEP_X[1], 0.0, "straight")] + bump2
    route = _routed_line("TV5", middle, step=(STEP_X[0], STEP_X[1], 0.30))
    return replace(route, groups=_spans(route, [
        ("stack M1: four optimal mitres (matched corners)", "matched", -FEATURE_X, -FEATURE_X + BUMP_W, BUMP_H),
        ("section: 0.30 mm, 8 mm (width mismatch)", "step", STEP_X[0], STEP_X[1], 0.0),
        ("stack U1: four unmitred corners (unmatched corners)", "unmatched", FEATURE_X - BUMP_W, FEATURE_X, BUMP_H),
    ]))


VARIANTS = {"TV1": tv1_step, "TV2": tv2_taper, "TV3": tv3_matched, "TV4": tv4_unmatched,
            "TV5": tv5_combined}


def straight_reference() -> Route:
    """TV0's own line as a route: it must reproduce TV0's copper (tested)."""
    return build_route("TV0", [(-PORT_X, 0.0, "end"), (PORT_X, 0.0, "end")], [TRACE_W])


# ================================================================ truth

def section_truth(route: Route) -> List[dict]:
    """Per section (a run of constant width along the route): arc interval, width, gap, the
    lidded Z0 from the generator's formula, and eps_eff. The gap is the one the section keeps
    (GAP everywhere on these lines)."""
    arc = route.arc_at_vertices()
    out = []
    for j, w in enumerate(route.widths):
        out.append({"s0": float(arc[j]), "s1": float(arc[j + 1]), "width": w, "gap": GAP,
                    "z0": z0_lidded(w, GAP), "eps_eff": lid_eps_eff(w, GAP)})
    merged: List[dict] = []
    for sec in out:
        if merged and abs(merged[-1]["width"] - sec["width"]) < 1e-12 and abs(merged[-1]["s1"] - sec["s0"]) < 1e-9:
            merged[-1]["s1"] = sec["s1"]
        else:
            merged.append(dict(sec))
    return merged


def features(route: Route) -> List[dict]:
    """The features the TDR should see on this line, by arc length from port 1: each corner or
    bend (from the route's marks), and each width step."""
    arc = route.arc_at_vertices()
    feats = [{"s": s, "kind": label} for s, label in route.marks]
    for m, joint in enumerate(route.joints, start=1):
        if joint == "straight" and abs(route.widths[m - 1] - route.widths[m]) > 1e-12:
            feats.append({"s": float(arc[m]), "kind": "width step %.2f -> %.2f mm" % (
                route.widths[m - 1], route.widths[m])})
    return sorted(feats, key=lambda f: f["s"])


def corner_prediction(width: float = TRACE_W, gap: float = GAP) -> dict:
    """The excess capacitance of an uncompensated 90 degree corner, bracketed two ways, and what
    it does at F_MAX_GHZ against the Born threshold tdr.py uses.

    Line capacitance per length C' = 1 / (Z0 v), with v = c / sqrt(eps_eff). The corner adds
    a fraction k of C' W with k between 0.1 and 0.3 (a guess bracket: no closed form was
    available to check against), and the parallel-plate bound eps0 eps_r W^2 / h is the most a
    full W x W square could add. A lumped shunt C gives |Gamma| = omega C Z0 / 2 at f_max."""
    z0 = z0_lidded(width, gap)
    eps = lid_eps_eff(width, gap)
    v_mm_per_ns = 299.792458 / math.sqrt(eps)
    c_per_mm_fF = 1e6 / (z0 * v_mm_per_ns)  # 1 / (Z0 * v[mm/s]) in fF/mm
    parallel_plate = 8.854e-12 * EPS_R / (H_SUB * 1e-3) * (width * 1e-3) ** 2 * 1e15  # fF
    lo, hi = 0.1 * c_per_mm_fF * width, 0.3 * c_per_mm_fF * width
    omega = 2 * math.pi * F_MAX_GHZ * 1e9
    beta = omega * math.sqrt(eps) / 299.792458e9  # rad/mm
    born_area = z0 * THRESHOLD / beta  # ohm mm
    c_thr = 2.0 * THRESHOLD / (omega * z0) * 1e15  # fF: the lumped C that reaches the threshold

    def gamma(c_fF: float) -> float:
        return omega * c_fF * 1e-15 * z0 / 2.0

    return {
        "width": width, "gap": gap, "z0": z0, "eps_eff": eps, "c_per_mm_fF": c_per_mm_fF,
        "c_range_fF": (lo, hi), "c_parallel_plate_fF": parallel_plate,
        "gamma_range": (gamma(lo), gamma(hi)), "gamma_plate": gamma(parallel_plate),
        "born_area_ohm_mm": born_area, "c_threshold_fF": c_thr, "beta_fmax_per_mm": beta,
    }


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write the TV1..TV5 copper.txt variants from TV0's.")
    parser.add_argument("--straight", required=True, help="TV0's copper.txt: the base for TV1 and TV2")
    parser.add_argument("--pocket", help="the 2.7 mm pocket parent's copper.txt: the base for TV3 to TV5 "
                                         "(default: --straight). It must agree with --straight outside the section.")
    parser.add_argument("--out", required=True, help="folder for <TVn>/copper.txt and variant.json")
    parser.add_argument("--only", nargs="*", help="variant names (default: all)")
    args = parser.parse_args(argv)
    with open(args.straight, encoding="utf-8") as f:
        straight = f.read()
    pocket = straight
    if args.pocket:
        with open(args.pocket, encoding="utf-8") as f:
            pocket = f.read()
        if section_free_lines(pocket) != section_free_lines(straight):
            print("test_lines: the pocket parent's copper differs from TV0's outside the section; "
                  "it is not the reference for these variants", file=sys.stderr)
            return 2
    for name, make in VARIANTS.items():
        if args.only and name not in args.only:
            continue
        route = make()
        base = straight if name in ("TV1", "TV2") else pocket
        folder = os.path.join(args.out, name)
        text, report = write_variant(base, route, os.path.join(folder, "copper.txt"))
        if name in ("TV1", "TV2"):
            parent_label = "TV0"
        elif args.pocket:
            parent_label = "TV0b (route_width 2.7)"
        else:
            parent_label = "TV0 copy (TV0b pending: copper written from TV0)"
        meta = {"name": name, "parent": parent_label,
                "length_mm": route.length, "lateral_extent_mm": route.lateral_extent(),
                "vertices": [list(p) for p in route.vertices], "widths": list(route.widths),
                "joints": list(route.joints), "groups": [list(g) for g in route.groups],
                "sections": section_truth(route), "features": features(route),
                "report": report.__dict__}
        with open(os.path.join(folder, "variant.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        print(f"{name}: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
