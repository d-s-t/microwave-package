"""
A 2-D field solve of the lid-covered CPW's cross-section, to check
`microwave_package.design.cpw` against.

`cpw.py` gives the 50 ohm gap in closed form (conformal mapping plus two
fitted terms). This solves the actual cross-section instead -- the copper as
a solid with its sidewalls, the substrate, the bottom ground, and the lid
with its relief channel and the channel's side walls -- by the finite-element
method on a graded rectangular grid, so the closed form can be tested
against something that does not assume its shortcuts. It is how the thickness
and cover terms were fitted, and `tests/test_cpw_field_check.py` keeps them
honest.

It needs numpy and scipy, which the project does not otherwise depend on
(`cpw.py` stays pure standard library for that reason), so it lives here
and not in the package, and the test skips without them.

    cross-section (y across the line, z up), mirror-symmetric about y = 0:

      z = -h .. 0     substrate (eps_r), bottom ground at z = -h
      z =  0 .. t     copper: signal for |y| < w/2, grounds for |y| > w/2 + gap
      lid             grounded metal: above z = t + hc for |y| < channel/2, and
                      above z = t beyond (the lid rests on the copper)
      |y| = half_width   a grounded wall, far away (the box)

Z0 comes from the two capacitances per unit length, with the substrate and
with it replaced by air: Z0 = 1 / (c sqrt(C C_air)), eps_eff = C / C_air.

    python base_parts/cpw_fd_check.py     # prints the comparison table
"""
import math
import os
import sys

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spl

C0 = 299792458.0
EPS0 = 8.8541878128e-12


def _graded(breaks, smallest, ratio):
    """A 1-D grid through `breaks`, its cells growing geometrically (by
    `ratio`, from about `smallest`) away from both ends of every segment, so
    that the metal edges, where the field is singular, get the fine cells."""
    points = [breaks[0]]
    for a, b in zip(breaks[:-1], breaks[1:]):
        length = b - a
        n = 1
        while smallest * (ratio ** n - 1.0) / (ratio - 1.0) < length / 2.0 and n < 400:
            n += 1
        cells = smallest * ratio ** np.arange(n)
        cells *= (length / 2.0) / cells.sum()
        cells = np.concatenate([cells, cells[::-1]])
        edges = a + np.concatenate([[0.0], np.cumsum(cells)])
        edges[-1] = b
        points.extend(edges[1:])
    return np.array(points)


def characteristic_impedance(trace_width, gap, substrate_height, eps_r, conductor_thickness,
                             channel_width, cover_height, half_width=4.0,
                             smallest_cell=0.004, growth=1.2):
    """(Z0 in ohm, eps_eff) of the covered CPW; millimetres throughout.

    `channel_width` and `cover_height` are the lid's relief channel (its
    width, and its depth above the top face of the copper). Make both large
    for an open top."""
    w, g, h, t = trace_width, gap, substrate_height, conductor_thickness
    half_width = max(half_width, channel_width / 2.0 + 1.0)
    y_cuts = sorted({0.0, w / 2, w / 2 + g, channel_width / 2, half_width})
    z_cuts = sorted({-h, 0.0, t, t + cover_height})
    y = _graded(np.array([v for v in y_cuts if 0.0 <= v <= half_width]), smallest_cell, growth)
    z = _graded(np.array(z_cuts), smallest_cell, growth)
    ny, nz = len(y), len(z)

    yc, zc = np.meshgrid(0.5 * (y[1:] + y[:-1]), 0.5 * (z[1:] + z[:-1]), indexing="ij")
    in_signal = (yc < w / 2) & (zc > 0) & (zc < t)
    in_ground = (yc > w / 2 + g) & (zc > 0) & (zc < t)
    in_lid = (zc > t + cover_height) | ((yc > channel_width / 2) & (zc > t))
    live = ~(in_signal | in_ground | in_lid)

    yn, zn = np.meshgrid(y, z, indexing="ij")
    tol = 1e-9
    on_signal = (yn <= w / 2 + tol) & (zn >= -tol) & (zn <= t + tol)
    on_ground = (
        ((yn >= w / 2 + g - tol) & (zn >= -tol) & (zn <= t + tol))
        | (zn <= -h + tol) | (yn >= half_width - tol) | (zn >= t + cover_height - tol)
        | ((yn >= channel_width / 2 - tol) & (zn >= t - tol))
    )
    on_signal &= ~on_ground
    fixed = (on_signal | on_ground).ravel()
    potential = on_signal.astype(float).ravel()

    index = np.arange(ny * nz).reshape(ny, nz)
    corners = np.stack([index[:-1, :-1], index[1:, :-1], index[1:, 1:], index[:-1, 1:]], axis=-1)
    touched = np.zeros(ny * nz, bool)
    touched[corners[live].ravel()] = True
    free = np.where(touched & ~fixed)[0]
    held = np.where(touched & fixed)[0]

    dy = np.diff(y)[:, None] * np.ones((1, nz - 1))
    dz = np.diff(z)[None, :] * np.ones((ny - 1, 1))
    # bilinear element on a rectangle: stiffness = eps (dz/dy * Ka + dy/dz * Kb) / 6
    ka = np.array([[2, -2, -1, 1], [-2, 2, 1, -1], [-1, 1, 2, -2], [1, -1, -2, 2]], float)
    kb = np.array([[2, 1, -1, -2], [1, 2, -2, -1], [-1, -2, 2, 1], [-2, -1, 1, 2]], float)

    def capacitance(eps_cells):
        k = ((dz / dy) * eps_cells / 6.0)[..., None, None] * ka \
            + ((dy / dz) * eps_cells / 6.0)[..., None, None] * kb
        nodes = corners[live]
        rows = np.repeat(nodes[:, :, None], 4, axis=2).ravel()
        cols = np.repeat(nodes[:, None, :], 4, axis=1).ravel()
        stiffness = sp.csr_matrix((k[live].ravel(), (rows, cols)), shape=(ny * nz, ny * nz))
        v = potential.copy()
        rhs = -stiffness[free][:, held] @ v[held]
        v[free] = spl.spsolve(stiffness[free][:, free].tocsc(), rhs)
        # energy of the half-domain, doubled for the mirror image
        return 2.0 * EPS0 * (v @ (stiffness @ v))

    eps_cells = np.where(zc < 0, eps_r, 1.0)
    c = capacitance(eps_cells)
    c_air = capacitance(np.ones_like(eps_cells))
    return 1.0 / (C0 * math.sqrt(c * c_air)), c / c_air


def gap_for_z0(trace_width, substrate_height, eps_r, conductor_thickness, channel_width,
               cover_height, z0=50.0, low=0.01, high=None, steps=22):
    """The gap that makes the field solve read `z0`, by bisection."""
    high = high or (channel_width - trace_width) / 2.0 - 0.05
    for _ in range(steps):
        mid = 0.5 * (low + high)
        z, _ = characteristic_impedance(trace_width, mid, substrate_height, eps_r,
                                        conductor_thickness, channel_width, cover_height)
        low, high = (mid, high) if z < z0 else (low, mid)
    return 0.5 * (low + high)


def _compare():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from microwave_package.design import cpw

    h, t = 0.381, 0.035
    print("Z0 in ohm: field solve against cpw.z0_gcpw, same geometry")
    print(f"{'board':>6} {'trace':>6} {'gap um':>7} {'channel':>8} {'relief um':>9} "
          f"{'field':>7} {'formula':>8} {'diff':>6}")
    cases = [
        (2.2, 0.4, 0.0943, 1.0, 0.2), (2.2, 0.4, 0.111, 1.0, 0.2), (2.2, 0.4, 0.111, 1.0, 0.04),
        (2.2, 0.4, 0.111, 1.0, 0.5), (2.2, 0.4, 0.111, 3.0, 3.0), (2.2, 0.5, 0.127, 1.0, 0.3),
        (9.8, 0.2, 0.247, 1.2, 0.3), (9.8, 0.2, 0.271, 1.0, 0.2), (9.8, 0.2, 0.271, 0.5, 0.04),
        (10.2, 0.15, 0.3, 2.0, 0.5), (4.4, 0.3, 0.15, 1.2, 0.2),
    ]
    for eps, w, g, wc, hc in cases:
        z_field, _ = characteristic_impedance(w, g, h, eps, t, wc, hc)
        z_formula = cpw.z0_gcpw(w, g, h, eps, t, cover_height=hc)
        print(f"{eps:6.1f} {w:6.2f} {g * 1000:7.1f} {wc:8.2f} {hc * 1000:9.0f} "
              f"{z_field:7.2f} {z_formula:8.2f} {100 * (z_formula / z_field - 1):+5.1f}%")

    print("\nThe 50 ohm gap in um: field solve against cpw.solve_gap_for_z0")
    for eps, w, wc, hc in [(2.2, 0.4, 1.0, 0.2), (2.2, 0.5, 1.2, 0.3), (9.8, 0.2, 1.2, 0.3), (9.8, 0.2, 2.0, 0.5)]:
        g_field = gap_for_z0(w, h, eps, t, wc, hc)
        g_formula = cpw.solve_gap_for_z0(w, h, eps, conductor_thickness=t, cover_height=hc)
        print(f"  eps {eps:4.1f} trace {w} channel {wc} relief {hc * 1000:.0f} um: "
              f"field {g_field * 1000:6.1f}  formula {g_formula * 1000:6.1f}")


if __name__ == "__main__":
    _compare()
