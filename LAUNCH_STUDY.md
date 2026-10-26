# Launch study

One SMP connector launch on its own, to find what lowers its reflection. On
the full package the two launches reflect and the 50 mm board between them is a
resonator (S21 ripple, S11 near -3 dB at 17 GHz); here one connector and a few
millimetres of board are cut with a matched 50 ohm CPW wave port, so S11 is
the launch alone.

Solution type `LaunchStudy` (`microwave_package/hfss/launch/`), chosen on the
run form like Line impedance. It needs no SolidWorks and no Altium: the copper
is drawn in Python (`launch/geometry.py`) and the model in HFSS
(`launch/build.py`), and analysis goes to the worker queue as a DrivenModal
task.

## The model

- Board `launch_trace_length_mm` (8) behind the launch's inner edge, so about
  20 mm long, `launch_half_width_mm` (3.5) either side of the line.
- The package's own stack seen from one end: box floor, substrate, copper (drawn
  by `copper_geometry.build`), the lid on the copper with its relief channel and
  the connector's window (`port_length` + 2 `smp_window_extra`, sharp-cornered
  here), the vendor SMP model placed by `model_builder.place_connectors`.
- The lid carries the real lid's **deeper last millimetre**: the relief channel is cut 0.4 mm
  (not the line's 0.3) over the 1.0 mm before each connector window, because the connector's
  centre-pin tab runs under that stretch to the pad. The numbers are read off the lid's STEP
  (`base_parts/lid_inside.SLDPRT`: ceiling at 0.3 mm to x = 12.65, 0.4 mm from there to the
  window's inner wall at 13.65, 1.2 mm wide). The first baseline, drawn without it, reflected
  everything: the tab touched the lid.
- Port 1 is the wave port across the connector's air bore (the `connector`
  reference plane of the full runs). Port 2 is a 50 ohm CPW wave port on the cut,
  `launch_port_half_width_mm` (1.5) either side of the line like the line check's
  section; the rest of the end face is a perfect conductor.
- 2-20 GHz, 73 points (0.25 GHz grid, which the time-domain read needs),
  adaptive at 20 GHz, interpolating sweep.
- The baseline copper is the real board's: `tests/test_launch_geometry.py`
  compares it, point by point, with the launch of a finished bare-board run
  (`tests/fixtures/launch_copper_tmm.txt`) -- the grid of top copper and the set
  of ground vias agree.

## Levers

| Lever | Parameter | Notes |
|---|---|---|
| Trace width (gap re-solved for 50 ohm under the lid) | `cpw_trace_width` | Only some traces are buildable: the build refuses a trace whose 50 ohm gap is unreachable under the 0.3 mm relief or under the 100 um minimum (`constraints.MIN_CPW_GAP`: AEDT cannot unite copper closer). **TMM 10i: 0.2 mm only** (0.3 tops out near 39 ohm). **RO3003: 0.4 and 0.48** (0.48 = the pad's width, the real "no step"). **RO4003: 0.35 and 0.4.** The full runs' own traces are the baselines: 0.4 (RO3003), 0.35 (RO4003), 0.2 (TMM). |
| Ground keep-out round the pad | `launch_clearance` | already in the package; flat below 10 GHz in the earlier TMM test |
| Pad width / length | `launch_pad_width`, `launch_pad_length` | 0 = the vendor's 0.48 x 1.4. Anything else is a **custom footprint** and needs the manufacturer's solderability / assembly check. A pad within 10 um of the gap edge draws no pull-back step (a sliver AEDT cannot mesh). |
| Taper from pad to trace | `launch_taper_length` | 0 = the step the package has; the taper runs from the pad's width to the trace's, just inboard of the pad |
| Anti-pad void in the top ground | `launch_void_radius` | 0 = the vendor's 2.3. Up to 2.58 (the land is 2.6 half-size); a bigger void splits the land and is refused. Custom footprint. A void past about 2.5 leaves the ground pads (centres 2.83 mm out) mostly in the void. |
| Hole in the bottom ground under the pad | `launch_bottom_cutout_radius` | 0 = none. A round hole through the bottom copper **and a matching pocket in the box floor** (a machined feature of the real box; here the model's whole 0.5 mm floor), vias kept out of it. |
| Lid relief over the launch | `launch_relief_depth` (also `route_clearence`, `route_width`) | a deeper channel from the board end to 0.5 mm past the pad, step and taper (the connector's window already removes the lid out to the land's far edge); 0 = the line's relief |
| Board permittivity | the run's PCB material | one run per substrate |

The launch_* parameters read 0 as "the vendor's", so a combination that does not
set them is the plain launch. The package's own board does not read them.

## Output

Per combination, beside the project: `results.csv` (frequency, S11 in dB and
degrees, real and imaginary part), `launch_summary.json` (band-mean S11 in
2-10 / 10-17 / 17-20 GHz, worst S11 and where, S11 at 5/10/15/17/20 GHz, the
time-domain peaks with distance and |Gamma|, the near-launch |Gamma| within
3 mm, the geometry), `smith.csv`. The run page shows a Launch table and the
plot picker offers the Smith chart of S11. The time-domain read is the one
`hfss/driven/s11_analysis.py` applies to the full runs (Kaiser window, distance
for an effective permittivity of (er + 1) / 2).

## Validating against the full runs

Compare in **time**, not distance: the round trip from the port plane, in ns.
The two signal pads are about 27 mm apart, so the spacing of the full run's two
reflections gives the line's effective permittivity (below); a distance
computed from an assumed (er + 1) / 2 is wrong for a ground-backed CPW, which
is why `launch_summary.json` reports `t_ns` first and its `distance_mm` only
"for the assumed eps_eff". Read on the study's own grid (2-20 GHz, coarse rows,
Kaiser window, beta 6), the full bare runs give:

| Substrate | Full run | Near launch (t, \|G\|) | Far launch (t, \|G\|) | eps_eff from 27 mm | Mean S11 2-10 / 10-17 / 17-20 GHz (dB) | Worst S11 |
|---|---|---|---|---|---|---|
| RO3003, trace 0.4 | 143807 | 0.019 ns, 0.329 | 0.316 ns, 0.282 | 2.72 | -13.6 / -8.1 / -6.9 | -2.0 dB at 20 GHz |
| RO4003, trace 0.35 | 145007 | 0.020 ns, 0.356 | 0.334 ns, 0.296 | 3.04 | -12.4 / -7.5 / -6.8 | -2.0 dB at 19 GHz |
| TMM 10i, trace 0.2 | 141347 | 0.039 ns, 0.467 | 0.505 ns, 0.284 | 6.69 | -7.4 / -5.6 / -1.5 | -0.2 dB at 17 GHz |

The launch-only study must show one peak at the near launch's time, +-0.02 ns
(about 0.5 mm on the line), with the near launch's \|G\| within 15 %; the far
peak must be absent. Its \|S11\| must sit at the mean of the full run's ripple
maximum and minimum. Only then are the sweep results worth reading. The TMM 10i
sweep is finer (0.05 GHz) than the other two (0.25 GHz); the read is the same.

## How to create the runs

The 12 combinations are each their own run (they differ in several parameters at
once and in substrate): TMM 10i baseline, taper 0.5, taper 1.0, pad width 0.30,
void radius 2.0, void radius 2.5, bottom cutout r 1.0, r 1.6; RO3003 baseline
(trace 0.4), trace 0.48, taper 0.5; RO4003 baseline (trace 0.35).

```
python -m microwave_package.launch_study_runs --dry-run            # print the 12 forms
python -m microwave_package.launch_study_runs --gate               # create the three baselines
python -m microwave_package.launch_study_runs --only tmm_taper_0p5 # one run
```

`microwave_package/launch_study_runs.py` posts the web app's own form with every
field explicit (nothing carried over from the latest run); the same 12 runs as
a spec are in `sweeps/examples/launch_study_runs.json`
(`{"runs": [{"name", "label", "substrate", "params": {name: "value"}}]}`,
the structure of `queue_runs.py`; the form carries each `params` entry as
`range_<name>`). Fields common to all: `solution_type=LaunchStudy`,
`no_chip=1`, the launch settings at their defaults (2-20 GHz, 73 points, delta S
0.02, 99/3/2 passes), `student_version`, `perfect_e_box` and
`perfect_e_conductors` ticked, `run_both` not sent; Altium is ticked and
ignored (the run forces it off). Per run only `mat_pcb`, `range_cpw_trace_width`
and the one lever (`range_launch_taper_length`, `range_launch_pad_width`,
`range_launch_void_radius`, `range_launch_bottom_cutout_radius`) differ. Create
the gate first, check each built model's `launch.json` (trace, gap, pad, taper,
void, board size) beside its project, and only then the other nine.

## Limits

The void is limited to the vendor's land (above); a launch whose ground lands
and slot are redrawn is not modelled. The CPW port on the cut can mis-solve
above about 14 GHz if the board beside the via fence resonates (the line check
found a 3 mm half-width collapsing there): the via fence is in the model for
that reason, and the baseline validation is the gate. A winning geometry still
has to be ported to `base_parts/altium/GenerateFromParams.pas`.
