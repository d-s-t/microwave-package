"""
The Altium half of geometry generation: hand a parameter set to a
DelphiScript running inside Altium, wait for it to say it is finished,
and report what it produced.

Altium has no COM server, so this drives it the only way there is --
launching X2.EXE with a ScriptingSystem process. Three facts learned in
`base_parts/altium/spikes` shape everything here:

  * The launcher is not the script. X2.EXE forwards the process to the
    Altium instance already running and the stub exits immediately, so
    its exit code says nothing at all about whether the script ran. The
    only trustworthy completion signal is a sentinel file the script
    writes itself.

  * There is no headless mode. A parse error inside the script raises a
    MODAL DIALOG that waits for a human forever; a runtime error parks
    it in the debugger. Either way the script never returns, which is
    why every call here is bounded by a timeout rather than a wait.

  * A cold start is slow and a warm one is not -- roughly a minute
    against three seconds -- because a warm session is reused rather
    than replaced. The first call of a sweep gets a much longer budget
    than the rest.

Parameters cross the boundary as a flat `key=value` text file.
DelphiScript has no JSON parser, and inventing one in Pascal to save a
few characters would be the wrong trade.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("eigen_mode_analyze")

# Where Altium is installed. The version segment moves with upgrades, so
# the search is ordered newest-first rather than pinned.
_ALTIUM_ROOTS = (
    r"C:\Program Files\Altium",
    r"C:\Program Files (x86)\Altium",
)

# A cold start pays for loading Altium itself; a warm one does not.
COLD_START_TIMEOUT = 420.0
WARM_TIMEOUT = 120.0
POLL_SECONDS = 2.0


def find_altium_exe() -> Optional[str]:
    """Path of the newest installed X2.EXE, or None if Altium is absent."""
    candidates = []
    for root in _ALTIUM_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            exe = os.path.join(root, name, "X2.EXE")
            if os.path.isfile(exe):
                candidates.append((name, exe))
    if not candidates:
        return None
    # "AD26" sorts after "AD25" lexicographically, which is what we want.
    return sorted(candidates)[-1][1]


def altium_is_running() -> bool:
    """Whether a session is already up -- decides the timeout budget, and
    nothing else. Never used to skip a launch: the process forwards
    itself either way."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq X2.EXE", "/NH"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return False
    return "X2.EXE" in out


def write_params(path: str, values: Dict[str, float], extra: Optional[Dict[str, str]] = None) -> None:
    """Write the parameter file the DelphiScript reads.

    Flat `key=value` lines, millimetres throughout, matching what
    `default.txt` holds -- no unit conversion anywhere in this path,
    because a conversion that exists in one direction only is how
    geometry quietly ends up a thousand times too large.
    """
    lines = [
        "# Written by eigen_mode_analyze.altium_generation -- do not edit by hand.",
        "# All lengths in millimetres.",
    ]
    for name in sorted(values):
        lines.append(f"{name}={values[name]!r}")
    for name, value in sorted((extra or {}).items()):
        lines.append(f"{name}={value}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def read_sentinel(path: str) -> Dict[str, str]:
    """Parse the script's `key=value` report. Missing file -> empty."""
    result: Dict[str, str] = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def run_script(
    script_path: str,
    procedure: str,
    sentinel_path: str,
    timeout: Optional[float] = None,
    log: Callable[[str], None] = logger.info,
) -> Tuple[bool, Dict[str, str]]:
    """Run `procedure` from `script_path` inside Altium and wait for it
    to finish. Returns (ok, report).

    Completion means the sentinel file exists and its last line says the
    script reached its end -- not that any process exited. A script that
    dies on a modal dialog leaves a partial sentinel, and that partial
    content is returned, because how far it got is the only diagnostic
    Altium leaves behind.
    """
    exe = find_altium_exe()
    if exe is None:
        raise RuntimeError(
            "Altium Designer not found -- looked for X2.EXE under "
            + " and ".join(_ALTIUM_ROOTS)
        )
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"Altium script not found: {script_path}")

    # A stale sentinel from a previous run would be read as this run's
    # result within microseconds of starting.
    if os.path.exists(sentinel_path):
        os.remove(sentinel_path)

    if timeout is None:
        timeout = WARM_TIMEOUT if altium_is_running() else COLD_START_TIMEOUT

    argument = (
        f'-RScriptingSystem:RunScriptFile(FileName="{script_path}"'
        f'|ProcName="{procedure}")'
    )
    log(f"Launching Altium: {procedure} (timeout {timeout:.0f}s)")
    subprocess.Popen([exe, argument], close_fds=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = read_sentinel(sentinel_path)
        if report.get("stage") == "end":
            ok = report.get("ok", "1") == "1"
            log(f"Altium script finished ({'ok' if ok else 'reported a failure'}).")
            return ok, report
        time.sleep(POLL_SECONDS)

    partial = read_sentinel(sentinel_path)
    if partial:
        log(
            "Altium script timed out. It got as far as: "
            + ", ".join(f"{k}={v}" for k, v in partial.items())
        )
    else:
        log("Altium script timed out having written nothing at all.")
    return False, partial


# --- assembling what the generator needs -------------------------------
#
# The equation file holds drawn dimensions. The layout generator needs a
# few things that are derived rather than drawn -- above all the CPW gap,
# which is solved for 50 ohm rather than typed, and solved separately per
# substrate because Rogers and silicon do not behave alike.

def build_params(
    values: Dict[str, float],
    mat_pcb: str = "Rogers TMM 10i (tm)",
    mat_chip: str = "silicon",
    target_z0: float = 50.0,
) -> Dict[str, float]:
    """Equation-file values plus the derived geometry Altium needs.

    Adds `pcb_cpw_gap` and `chip_cpw_gap`, each solved for `target_z0`
    on its own substrate with its own metal thickness. Both are recorded
    rather than recomputed downstream, so the run keeps a record of the
    geometry it actually built.
    """
    from . import cpw

    out = dict(values)
    trace = values["cpw_trace_width"]

    out["pcb_cpw_gap"] = cpw.solve_gap_for_z0(
        trace_width=trace,
        substrate_height=values["pcb_width"],
        eps_r=cpw.permittivity_of(mat_pcb),
        z0=target_z0,
        conductor_thickness=values.get("copper_thickness", 0.0),
    )
    out["chip_cpw_gap"] = cpw.solve_gap_for_z0(
        trace_width=trace,
        substrate_height=values["chip_width"],
        eps_r=cpw.permittivity_of(mat_chip),
        z0=target_z0,
        conductor_thickness=values.get("chip_metal_thickness", 0.0),
    )
    out["target_z0"] = target_z0
    return out


# --- the per-combination stage ----------------------------------------

def generate_for_combo(
    combo_dir: str,
    values: Dict[str, float],
    materials: Optional[Dict[str, str]] = None,
    log: Callable[[str], None] = logger.info,
) -> Tuple[bool, str]:
    """Regenerate the boards for one combination and leave a copper.txt
    in `combo_dir`. Returns (ok, reason).

    Two Altium calls rather than one: the generator writes the boards,
    then the exporter reads them back and dumps their copper. Keeping
    them separate means a layout can be regenerated and inspected
    without an export, and -- more usefully when something breaks -- the
    report says which of the two failed.

    A failure here is returned rather than raised. One combination whose
    layout cannot be built should be recorded and skipped, exactly as a
    failed STEP already is, not abort the whole sweep.
    """
    from . import paths

    materials = materials or {}
    params = build_params(
        values,
        mat_pcb=materials.get("mat_pcb", "Rogers TMM 10i (tm)"),
        mat_chip=materials.get("mat_chip", "silicon"),
    )
    write_params(
        str(paths.ALTIUM_PARAMS_FILE), params,
        extra={"out_dir": str(paths.ALTIUM_OUT_DIR)},
    )

    ok, report = run_script(
        str(paths.ALTIUM_GENERATOR), "GenerateFromParams",
        str(paths.ALTIUM_GENERATE_REPORT), log=log,
    )
    if not ok:
        return False, _describe(report, "Altium layout generation")

    ok, report = run_script(
        str(paths.ALTIUM_COPPER_EXPORTER), "ExportCopper",
        str(paths.ALTIUM_COPPER_REPORT), log=log,
    )
    if not ok:
        return False, _describe(report, "Altium copper export")

    source = paths.ALTIUM_OUT_DIR / "copper.txt"
    if not source.is_file():
        return False, "Altium reported success but wrote no copper.txt"

    os.makedirs(combo_dir, exist_ok=True)
    destination = os.path.join(combo_dir, "copper.txt")
    with open(source, "r", encoding="utf-8") as src:
        content = src.read()
    with open(destination, "w", encoding="utf-8") as dst:
        dst.write(content)

    # The copper belongs to this combination, so it is kept with it
    # rather than left in the shared output folder to be overwritten by
    # the next combination.
    log(f"Copper for this combination written to {destination}")
    return True, ""


def _describe(report: Dict[str, str], what: str) -> str:
    """Turn a partial sentinel into a sentence worth reading on the run
    page. How far the script got is the only diagnostic Altium gives."""
    if not report:
        return f"{what} produced no output at all -- Altium may be showing a modal dialog."
    if report.get("error"):
        return f"{what} failed: {report['error']}"
    reached = ", ".join(f"{k}={v}" for k, v in report.items())
    return f"{what} did not finish. It got as far as: {reached}"
