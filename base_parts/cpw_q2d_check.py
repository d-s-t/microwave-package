"""
AEDT 2D Extractor reference for the CPW impedance in `microwave_package/design/cpw.py`.

The closed form (`cpw.py`) is checked against two solvers written for this project
(`cpw_fd_check.py`, finite element; `cpw_bem_check.py`, boundary element). This is the
third check, and the one that is not ours: ANSYS's own 2D Extractor on the same cross-section.
For each case it builds the section in a Q2D design (bottom ground, the board, the copper
trace and coplanar grounds with their real thickness, and for the covered cases the lid as a
ceiling plus the walls of its relief channel), solves it, exports the capacitance and
inductance matrices and prints Z0 = sqrt(L/C) beside the other three numbers.

Run 2026-10-11 (AEDT 2025 R2, Student), Z0 in ohm; the closed-form and finite-element columns are
the ones in `tests/test_cpw_field_check.py`'s tolerances:

    Q2D    FE   formula  case
  62.39 62.44   61.77   5880 0.4 mm, 111 um gap, open top        (the old solve called this 50)
  51.99 52.03   51.75   5880 0.4 mm, 111 um gap, lid 1.0 mm x 200 um   (B17)
  50.29 50.29   50.00   5880 0.4 mm,  94 um gap, lid 1.0 mm x 200 um
  56.52 56.55   56.10   5880 0.4 mm, 111 um gap, lid 1.0 mm x 300 um   (B17)
  54.55 54.87   54.47   TMM 10i 0.2 mm, 271 um gap, open top   (the old solve called this 50)
  50.15 50.13   50.00   TMM 10i 0.2 mm, 247 um gap, default lid 1.2 mm x 300 um
  23.78 23.51   24.75   5880 0.4 mm, 111 um gap, old default lid 0.5 mm x 40 um   (B13)
  50.38 50.38   50.00   5880 0.5 mm, 106 um gap, default lid 1.2 mm x 300 um
  50.17 50.23   50.01   5880 0.45 mm, 129 um gap, lid 1.0 mm x 200 um   (B23)

It starts its own AEDT, and the Student licence runs one at a time, so close AEDT first.
Needs the project's venv (pyaedt), numpy and scipy:

    .venv\\Scripts\\python.exe base_parts\\cpw_q2d_check.py [case index ...]
"""
import math
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

H, T = 0.381, 0.035
# (label, material, eps_r, trace, gap, channel width or None for open top, relief)
CASES = [
    ("5880 0.4 mm, 111 um gap, open top", "Rogers RT/duroid 5880 (tm)", 2.2, 0.4, 0.111, None, None),
    ("5880 0.4 mm, 111 um gap, lid 1.0 mm x 200 um", "Rogers RT/duroid 5880 (tm)", 2.2, 0.4, 0.111, 1.0, 0.2),
    ("5880 0.4 mm, 94 um gap, lid 1.0 mm x 200 um", "Rogers RT/duroid 5880 (tm)", 2.2, 0.4, 0.0943, 1.0, 0.2),
    ("5880 0.4 mm, 111 um gap, lid 1.0 mm x 300 um", "Rogers RT/duroid 5880 (tm)", 2.2, 0.4, 0.111, 1.0, 0.3),
    ("TMM 10i 0.2 mm, 271 um gap, open top", "Rogers TMM 10i (tm)", 9.8, 0.2, 0.271, None, None),
    ("TMM 10i 0.2 mm, 247 um gap, default lid 1.2 mm x 300 um", "Rogers TMM 10i (tm)", 9.8, 0.2, 0.2474, 1.2, 0.3),
    ("5880 0.4 mm, 111 um gap, old default lid 0.5 mm x 40 um", "Rogers RT/duroid 5880 (tm)", 2.2, 0.4, 0.111, 0.5, 0.04),
    ("5880 0.5 mm, 106 um gap, default lid 1.2 mm x 300 um", "Rogers RT/duroid 5880 (tm)", 2.2, 0.5, 0.1063, 1.2, 0.3),
    ("5880 0.45 mm, 129 um gap, lid 1.0 mm x 200 um", "Rogers RT/duroid 5880 (tm)", 2.2, 0.45, 0.129, 1.0, 0.2),
]


def reference_numbers(eps, trace, gap, channel, relief):
    import cpw_fd_check as fd
    from microwave_package.design import cpw
    out = {"formula": cpw.z0_gcpw(trace, gap, H, eps, T, cover_height=relief)}
    out["fd"] = fd.characteristic_impedance(trace, gap, H, eps, T, channel or 12.0, relief or 12.0)[0]
    if channel is None:
        import cpw_bem_check as bem
        out["bem"] = bem.solve(trace, gap, H, eps, T)[0]
    return out


def build_case(q2d, material, trace, gap, channel, relief):
    """The cross-section in the XY plane: x across the line, y up. Millimetres."""
    m = q2d.modeler
    m.model_units = "mm"
    half = 4.0                      # half-width of everything (the box side is far away)
    a, b = trace / 2.0, trace / 2.0 + gap

    ground = m.create_rectangle([-half, -H - T, 0], [2 * half, T], name="bottom_ground", material="copper")
    m.create_rectangle([-half, -H, 0], [2 * half, H], name="substrate", material=material)
    signal = m.create_rectangle([-a, 0, 0], [trace, T], name="trace", material="copper")
    left = m.create_rectangle([-half, 0, 0], [half - b, T], name="ground_left", material="copper")
    right = m.create_rectangle([b, 0, 0], [half - b, T], name="ground_right", material="copper")
    grounds = [ground, left, right]
    if channel is not None:
        top = T + relief
        # the lid: a ceiling over the channel, and walls either side of it that rest on the copper
        grounds.append(m.create_rectangle([-half, top, 0], [2 * half, 1.0], name="lid_ceiling", material="copper"))
        wall = half - channel / 2.0
        grounds.append(m.create_rectangle([-half, T, 0], [wall, relief], name="lid_wall_left", material="copper"))
        grounds.append(m.create_rectangle([channel / 2.0, T, 0], [wall, relief], name="lid_wall_right", material="copper"))
    q2d.assign_single_conductor(assignment=[signal], name="signal", conductor_type="SignalLine",
                                solve_option="SolveInside", units="mm")
    q2d.assign_single_conductor(assignment=grounds, name="gnd", conductor_type="ReferenceGround",
                                solve_option="SolveInside", units="mm")


def main(indices):
    from ansys.aedt.core import Q2d
    from microwave_package.hfss import model_builder

    version = model_builder.aedt_version_for(None)
    folder = tempfile.mkdtemp(prefix="q2d_check_")
    project = os.path.join(folder, "q2d_check.aedt")
    q2d = None
    results = []
    try:
        for index in indices:
            label, material, eps, trace, gap, channel, relief = CASES[index]
            if q2d is None:
                q2d = Q2d(project=project, design=f"case{index}", solution_type="Open", version=version,
                          non_graphical=True, new_desktop=True, student_version=True)
            else:
                q2d.insert_design(f"case{index}")
            build_case(q2d, material, trace, gap, channel, relief)
            setup = q2d.create_setup("Setup1")
            setup.props["AdaptiveFreq"] = "1GHz"
            setup.update()
            q2d.analyze_setup("Setup1")
            files = {}
            for kind in ("CG", "RL"):      # capacitance, and resistance / inductance
                path = os.path.join(folder, f"case{index}_{kind}.txt")
                q2d.export_matrix_data(path, problem_type=kind, freq="1", freq_unit="GHz")
                files[kind] = path
            ref = reference_numbers(eps, trace, gap, channel, relief)
            c_pf = float(re.search(r"Capacitance Matrix\s+signal\s+signal\s+([-+0-9.Ee]+)",
                                   open(files["CG"]).read()).group(1))
            l_nh = float(re.search(r"Inductance Matrix\s+signal\s+signal\s+([-+0-9.Ee]+)",
                                   open(files["RL"]).read()).group(1))
            z0 = math.sqrt(l_nh * 1e-9 / (c_pf * 1e-12))
            print(f"case {index}: {label}: Q2D C = {c_pf:.3f} pF/m, L = {l_nh:.3f} nH/m -> Z0 = {z0:.2f} ohm")
            results.append((label, z0, ref))
    finally:
        if q2d is not None:
            q2d.release_desktop()
    print("\n   Q2D     FE     BEM  formula  case")
    for label, z0, ref in results:
        bem = f"{ref['bem']:7.2f}" if "bem" in ref else "      -"
        print(f"{z0:7.2f} {ref['fd']:7.2f} {bem} {ref['formula']:8.2f}  {label}")


if __name__ == "__main__":
    # It starts AEDT, so it takes the same one-at-a-time lock as the pipeline.
    from microwave_package.hfss import model_builder
    model_builder.one_aedt_at_a_time(main)([int(x) for x in sys.argv[1:]] or list(range(len(CASES))))
