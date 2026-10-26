"""Create the launch study's runs in the web app, every form field explicit.

The web form opens on the latest run's settings and an unticked box is simply
absent from a POST, so a run made by clicking inherits whatever the last one
had. Here nothing is inherited: the settings are the registry's own defaults,
the materials and ticks are spelled out, and each combination of the study is
its own run (they differ in several parameters at once and in substrate, so
they are not one sweep).

    python -m microwave_package.launch_study_runs --dry-run
    python -m microwave_package.launch_study_runs --only tmm_baseline
    python -m microwave_package.launch_study_runs --spec my_spec.json --base http://127.0.0.1:5000

Create the validation gate first (`--gate`: the three baselines) and the rest
only if they pass; see LAUNCH_STUDY.md. Each POST starts the run, so create
one, wait for its model build and check it, then the next (`--gap` seconds are
left between runs, not enough to check anything).

A spec is JSON: {"runs": [{"name", "label", "substrate", "params": {name: "value"}}]}.
`--write-spec PATH` writes the built-in one.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

TMM = "Rogers TMM 10i (tm)"
RO3003 = "Rogers RO3003 (tm)"
RO4003 = "Rogers RO4003 (tm)"

MATERIALS = {
    "mat_box": "aluminum", "mat_pcb": TMM, "mat_chip": "silicon", "mat_vacuum": "vacuum",
    "mat_conductor_pcb": "copper", "mat_conductor_chip": "niobium", "mat_wirebond": "aluminum",
}
# Ticked, as the full bare runs the study is validated against had them. Altium is ticked too
# and ignored: the launch study draws its own copper, and the run forces it off.
TICKED = ["student_version", "use_altium", "perfect_e_box", "perfect_e_conductors", "include_wirebonds"]

# The baselines are the full bare runs' own traces: RO3003 0.4, RO4003 0.35, TMM 0.2 mm.
BASELINE_TRACE = {TMM: "0.2", RO3003: "0.4", RO4003: "0.35"}


def _run(name: str, label: str, substrate: str, **params: str) -> Dict[str, Any]:
    trace = params.pop("cpw_trace_width", BASELINE_TRACE[substrate])
    return {"name": name, "label": label, "substrate": substrate,
            "params": {"cpw_trace_width": trace, **params}}


# The 12 combinations, TMM first. The gate is the three baselines.
RUNS: List[Dict[str, Any]] = [
    _run("tmm_baseline", "launch TMM baseline", TMM),
    _run("ro3003_baseline", "launch RO3003 baseline", RO3003),
    _run("ro4003_baseline", "launch RO4003 baseline", RO4003),
    _run("tmm_taper_0p5", "launch TMM taper 0.5", TMM, launch_taper_length="0.5"),
    _run("tmm_taper_1p0", "launch TMM taper 1.0", TMM, launch_taper_length="1.0"),
    _run("tmm_pad_0p30", "launch TMM pad width 0.30", TMM, launch_pad_width="0.3"),
    _run("tmm_void_2p0", "launch TMM void radius 2.0", TMM, launch_void_radius="2.0"),
    _run("tmm_void_2p5", "launch TMM void radius 2.5", TMM, launch_void_radius="2.5"),
    _run("tmm_cutout_1p0", "launch TMM bottom cutout r 1.0", TMM, launch_bottom_cutout_radius="1.0"),
    _run("tmm_cutout_1p6", "launch TMM bottom cutout r 1.6", TMM, launch_bottom_cutout_radius="1.6"),
    _run("ro3003_trace_0p48", "launch RO3003 trace 0.48", RO3003, cpw_trace_width="0.48"),
    _run("ro3003_taper_0p5", "launch RO3003 taper 0.5", RO3003, launch_taper_length="0.5"),
]
GATE = ["tmm_baseline", "ro3003_baseline", "ro4003_baseline"]


def build_form(run: Dict[str, Any]) -> List[tuple]:
    """The POST body for one run: label, solution type, every analysis setting
    the registry knows (its defaults, the solver presets' converged values for
    the pass counts), the materials, the ticks, and a `range_<name>` per pinned
    parameter."""
    from .hfss import registry

    pairs: List[tuple] = [("label", run["label"][:80]), ("solution_type", "LaunchStudy"),
                          ("aedt_version", "")]
    settings = {**registry.all_defaults(), "poll_seconds": 10.0, **run.get("settings", {})}
    for key, value in settings.items():
        if key in ("solution_type", "run_both"):
            continue
        pairs.append((key, "" if value is None else str(value)))
    materials = {**MATERIALS, **({"mat_pcb": run["substrate"]} if run.get("substrate") else {})}
    pairs += list(materials.items())
    pairs += [(key, "on") for key in TICKED]
    # NOT sent on purpose: run_both, graphical.
    for name, spec in run.get("params", {}).items():
        pairs.append((f"range_{name}", str(spec)))
    # The bare board: no die, opening or bonds, and a flat floor (the study has none of them).
    pairs.append(("no_chip", "1"))
    return pairs


def post_run(base: str, run: Dict[str, Any]) -> str:
    data = urllib.parse.urlencode(build_form(run)).encode()
    request = urllib.request.Request(base + "/runs/new", data=data, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        final = response.geturl()
    parsed = urllib.parse.urlparse(final)
    query = urllib.parse.parse_qs(parsed.query)
    if "error" in query:
        raise RuntimeError(query["error"][0])
    return parsed.path.rsplit("/", 1)[-1]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spec", help="a JSON spec instead of the built-in 12 runs")
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    parser.add_argument("--only", nargs="*", help="run names to create")
    parser.add_argument("--gate", action="store_true", help="only the three baselines")
    parser.add_argument("--dry-run", action="store_true", help="print the forms, post nothing")
    parser.add_argument("--write-spec", metavar="PATH", help="write the built-in spec and exit")
    parser.add_argument("--gap", type=float, default=15.0, help="seconds between runs")
    args = parser.parse_args(argv)

    if args.write_spec:
        with open(args.write_spec, "w", encoding="utf-8") as handle:
            json.dump({"runs": RUNS}, handle, indent=2)
            handle.write("\n")
        return 0
    runs = RUNS
    if args.spec:
        with open(args.spec, encoding="utf-8") as handle:
            runs = json.load(handle)["runs"]
    wanted = set(args.only or []) | (set(GATE) if args.gate else set())
    runs = [r for r in runs if not wanted or r["name"] in wanted]
    for index, run in enumerate(runs):
        if args.dry_run:
            print(run["name"], json.dumps(build_form(run)))
            continue
        try:
            run_id = post_run(args.base, run)
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"FAILED {run['name']}: {exc}")
            return 1
        print(f"{run['name']} -> {run_id}", flush=True)
        if index < len(runs) - 1:
            time.sleep(args.gap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
