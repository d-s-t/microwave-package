"""
A second, independent solve of the thick-copper CPW, to check `cpw.py` and
`cpw_fd_check.py` against: a boundary-element (method-of-moments) solve with no grid.

`cpw_fd_check.py` meshes the whole cross-section. This does not: it puts piecewise-constant
charge on the conductor surfaces only (the trace with its sidewalls, and both ground coppers),
and uses the exact Green's function of a line charge over a dielectric slab on a ground plane,
so the substrate, the backing ground and the finite ground width are in it exactly and the
thickness of the metal is nothing but geometry. It was written because the field solve and the
old thickness formula disagreed by 25 % on RT/duroid 5880 and one solver is not evidence.
Open top only (no lid): the lid is `cpw_fd_check.py`'s job.

Needs numpy (not scipy), which the project does not otherwise depend on.

Green's function. A line charge q at height d >= 0 above a slab (eps_r, thickness h) on a
ground plane. In Fourier space (k the transverse wavenumber) the potential above the slab is
  q / (2 eps0 k) [ exp(-k|z-d|) + R(k) exp(-k(z+d)) ],   R = (1 - eps_r coth kh) / (1 + eps_r coth kh)
With u = exp(-2kh) and K = (1 - eps_r)/(1 + eps_r):   R = K - (1 - K^2) sum_{n>=1} K^(n-1) u^n.
Each exp(-ks)/k term transforms back to -ln(distance)/(2 pi eps0), so the potential is the charge's
own plus a series of image line charges:
  Phi = -(q / (2 pi eps0)) [ ln r0 + K ln r1 - (1-K^2) sum_{n>=1} K^(n-1) ln r(n+1) ]
with r0 the distance to the charge, r1 to its mirror image in the interface, and r(n+1) to that
image shifted down by 2 n h. The image charges sum to -q: the ground plane.

    python base_parts/cpw_bem_check.py     # prints the comparison table
"""
import math

import numpy as np

C0 = 299792458.0
EPS0 = 8.8541878128e-12


def graded_split(a, b, smallest=0.002, ratio=1.25):
    """Split [a, b] into panels, small at both ends and growing toward the middle."""
    length = b - a
    n = 1
    while smallest * (ratio ** n - 1.0) / (ratio - 1.0) < length / 2.0 and n < 300:
        n += 1
    cells = smallest * ratio ** np.arange(n)
    cells *= (length / 2.0) / cells.sum()
    cells = np.concatenate([cells, cells[::-1]])
    edges = a + np.concatenate([[0.0], np.cumsum(cells)])
    edges[-1] = b
    return edges


def panels_on(p0, p1, smallest, ratio):
    edges = graded_split(0.0, 1.0, smallest / max(math.dist(p0, p1), 1e-12), ratio)
    pts = [(p0[0] + e * (p1[0] - p0[0]), p0[1] + e * (p1[1] - p0[1])) for e in edges]
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def seg_log_integral(px, pz, ax, az, bx, bz):
    """integral over the segment A->B of ln(distance to P) d(length); arrays broadcast."""
    dx, dz = bx - ax, bz - az
    length = np.hypot(dx, dz)
    ex, ez = dx / length, dz / length
    s0 = (ax - px) * ex + (az - pz) * ez
    s1 = (bx - px) * ex + (bz - pz) * ez
    p = np.abs((ax - px) * ez - (az - pz) * ex)

    def prim(s):
        # integral of 0.5 ln(s^2 + p^2) ds = 0.5 [ s ln(s^2+p^2) - 2 s + 2 p atan(s/p) ]
        r2 = s * s + p * p
        with np.errstate(divide="ignore", invalid="ignore"):
            lg = np.where(r2 > 0, np.log(np.where(r2 > 0, r2, 1.0)), 0.0)
            at = np.where(p > 1e-300, p * np.arctan2(s, np.where(p > 1e-300, p, 1.0)), 0.0)
        return 0.5 * (s * lg - 2.0 * s + 2.0 * at)

    return prim(s1) - prim(s0)


def solve(w, gap, h, eps_r, t, ground_width=3.0, smallest=0.002, ratio=1.25, images=None):
    a = w / 2.0
    b = a + gap
    # conductor outlines: (name, potential, p0, p1)
    sides = []
    # trace: bottom, right, top, left
    sides += [("trace", 1.0, (-a, 0.0), (a, 0.0)), ("trace", 1.0, (a, 0.0), (a, t)),
              ("trace", 1.0, (a, t), (-a, t)), ("trace", 1.0, (-a, t), (-a, 0.0))]
    for sign in (1.0, -1.0):
        y0, y1 = sign * b, sign * (b + ground_width)
        sides += [("gnd", 0.0, (y0, 0.0), (y1, 0.0)), ("gnd", 0.0, (y1, 0.0), (y1, t)),
                  ("gnd", 0.0, (y1, t), (y0, t)), ("gnd", 0.0, (y0, t), (y0, 0.0))]
    panels = []
    for name, v, p0, p1 in sides:
        for q0, q1 in panels_on(p0, p1, smallest, ratio):
            panels.append((name, v, q0, q1))
    M = len(panels)
    ax = np.array([p[2][0] for p in panels]); az = np.array([p[2][1] for p in panels])
    bx = np.array([p[3][0] for p in panels]); bz = np.array([p[3][1] for p in panels])
    mx, mz = 0.5 * (ax + bx), 0.5 * (az + bz)
    length = np.hypot(bx - ax, bz - az)
    volt = np.array([p[1] for p in panels])
    is_trace = np.array([p[0] == "trace" for p in panels])

    def capacitance(eps):
        K = (1.0 - eps) / (1.0 + eps)
        # images: (weight, z-map) with z -> -z - shift
        terms = [(1.0, None), (K, 0.0)]
        if abs(K) < 1e-12:
            terms = [(1.0, None), (-1.0, 2.0 * h)]
        else:
            n_img = images or int(min(400, max(10, math.ceil(math.log(1e-12) / math.log(abs(K))))))
            for n in range(1, n_img + 1):
                terms.append((-(1.0 - K * K) * K ** (n - 1), 2.0 * n * h))
        A = np.zeros((M, M))
        PX, PZ = mx[:, None], mz[:, None]
        for wgt, shift in terms:
            if shift is None:
                Ax, Az, Bx, Bz = ax[None, :], az[None, :], bx[None, :], bz[None, :]
            else:
                Ax, Az, Bx, Bz = ax[None, :], -az[None, :] - shift, bx[None, :], -bz[None, :] - shift
            A += wgt * seg_log_integral(PX, PZ, Ax, Az, Bx, Bz)
        A *= -1.0 / (2.0 * math.pi * EPS0)
        sigma = np.linalg.solve(A, volt)
        return float(np.sum(sigma[is_trace] * length[is_trace]))

    c = capacitance(eps_r)
    c_air = capacitance(1.0)
    return 1.0 / (C0 * math.sqrt(c * c_air)), c / c_air


def _compare():
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from microwave_package.design import cpw

    h = 0.381
    print("Z0 in ohm, open top: boundary-element solve against cpw.z0_gcpw")
    print(f"{'board':>6} {'trace':>6} {'gap um':>7} {'metal um':>8} {'BEM':>7} {'formula':>8} {'diff':>6}")
    for eps, w, g, t in ((9.8, 0.2, 0.15, 0.0005), (2.2, 0.4, 0.111, 0.0005),
                         (2.2, 0.4, 0.111, 0.035), (2.2, 0.4, 0.094, 0.035), (2.2, 0.5, 0.127, 0.035),
                         (9.8, 0.2, 0.271, 0.035), (9.8, 0.2, 0.183, 0.035), (2.2, 0.4, 0.111, 0.070)):
        z, _ = solve(w, g, h, eps, t)
        f = cpw.z0_gcpw(w, g, h, eps, t)
        print(f"{eps:6.1f} {w:6.2f} {g * 1000:7.1f} {t * 1000:8.1f} {z:7.2f} {f:8.2f} {100 * (f / z - 1):+5.1f}%")


if __name__ == "__main__":
    _compare()
