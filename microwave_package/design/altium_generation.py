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

And one learned from a sweep that lost its first combination to a script
an earlier run had given up on:

  * Giving up on a script does not stop it, and Altium does not hold a
    new one back until it ends. A script launched while another is
    running waits in its launcher only until the running one next lets
    Windows messages through -- closing a document is enough -- and is
    then run right there, nested inside it. Watched on AD26: a second
    generator started as the first closed its package board, drew and
    saved its own, and handed back to the first, which finished by
    saving over the second's chip. So nothing new is launched until the
    earlier script's report says it has ended -- see `_wait_until_idle`.

Parameters cross the boundary as a flat `key=value` text file.
DelphiScript has no JSON parser, and inventing one in Pascal to save a
few characters would be the wrong trade.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Dict, Optional, Tuple

import psutil

logger = logging.getLogger("microwave_package")

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

# How long a launch waits for Altium to finish a script launched before
# it: the same budget that script had to run in, over again. Past that it
# is sitting on a dialog more often than it is merely slow. A report
# nobody has written for this long is taken to belong to a script that
# is no longer running at all -- stopped by hand at a dialog, say.
BUSY_TIMEOUT = WARM_TIMEOUT
# How often that wait asks again with a fresh launcher -- see
# `_wait_until_idle` for why it is not left to one.
PROBE_RETRY = 30.0

# One Altium job at a time within this process. The web app runs each
# run's pipeline in a thread of its own, and two runs drawing layouts at
# once share one params.txt, one set of reports and one generated/
# folder: the second's parameters land while the first's script has yet
# to read them. Reentrant, because a job holds it across `run_script`,
# which takes it as well.
_ALTIUM_IN_USE = threading.RLock()


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
        "# Written by microwave_package.altium_generation -- do not edit by hand.",
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
    """Parse the script's `key=value` report. Missing -> empty.

    Read in one go and closed at once, and a file that cannot be read is
    treated exactly like a missing one -- see `_settled` for why holding
    this file open for any longer than it takes to read it is something
    to avoid rather than to retry.
    """
    result: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return result
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _fingerprint(path: str):
    """Size and modification time, or None if the file is not there.

    Deliberately `stat` rather than a read: it says whether the script
    has written anything since the last look without opening the file.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _clear_sentinel(path: str, timeout: float = 30.0) -> bool:
    """Remove the previous call's report, waiting for Altium to let go.

    A stale sentinel would be read as this call's result within
    microseconds of starting, so it has to go -- but the script that
    wrote it may still have it open, and on Windows that refuses the
    delete.

    A delete going through does NOT show that script has finished: it
    only holds the file for the instant each progress line is written,
    and writes it again, whole, on the next. That is `_wait_until_idle`'s
    job, and it has been done before this is called.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def _launch(exe: str, script_path: str, procedure: str) -> subprocess.Popen:
    """Hand `procedure` from `script_path` to Altium. Returns the launcher.

    With Altium already running, what comes back is a stub that forwards
    the request and exits once Altium takes it: at once if Altium is
    free, and otherwise not before a running script next lets messages
    through, or a dialog closes. With Altium not running, it is Altium
    itself, and it does not exit at all.
    """
    argument = (
        f'-RScriptingSystem:RunScriptFile(FileName="{script_path}"'
        f'|ProcName="{procedure}")'
    )
    # Passed as ONE verbatim command string, not as a list. Given a
    # list, Windows applies MSVC quoting rules and escapes the quotes
    # this argument depends on; Altium then sees a broken FileName and
    # resolves the wreckage against its working directory, producing a
    # doubled drive letter. It fails as a modal dialog, so the mistake
    # costs a full timeout every time.
    return subprocess.Popen('"' + exe + '" ' + argument, close_fds=True)


def _is_launcher_stub(launcher: subprocess.Popen) -> bool:
    """Whether `launcher` is a stub forwarding to an Altium that was
    already running, rather than Altium itself.

    Only a stub may ever be killed: killing Altium would take whatever
    the operator has open in it along with it. Told apart by age, since a
    stub always has an older X2.EXE to forward to and Altium never does.
    """
    try:
        me = psutil.Process(launcher.pid)
        if me.name().upper() != "X2.EXE":
            return False
        born = me.create_time()
        for other in psutil.process_iter(["name", "create_time"]):
            if (
                other.pid != launcher.pid
                and (other.info["name"] or "").upper() == "X2.EXE"
                and other.info["create_time"] < born
            ):
                return True
    except psutil.Error:
        return False
    return False


def _withdraw(launcher: subprocess.Popen) -> bool:
    """Take back a request Altium has not started yet. True if it did.

    Killing a stub that is still waiting withdraws its request: tried
    against AD26, the script it carried never ran. A stub that has
    already exited has handed its script over, and nothing takes that
    back.
    """
    if launcher.poll() is not None or not _is_launcher_stub(launcher):
        return False
    launcher.kill()
    try:
        launcher.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return True


def _altium_started() -> Optional[float]:
    """When the running Altium started (epoch seconds), or None if none
    is. The oldest X2.EXE: every other one is a stub forwarding to it."""
    born = [
        p.info["create_time"]
        for p in psutil.process_iter(["name", "create_time"])
        if (p.info["name"] or "").upper() == "X2.EXE"
    ]
    return min(born) if born else None


def _still_running(report: str) -> bool:
    """Whether the script that writes `report` may not have finished.

    Every script here writes `stage=start` the moment it starts and
    `stage=end` as its very last act, so a report without the end is a
    script still going -- or one that never will be: stopped by hand at a
    dialog, or run by an Altium that has since closed. Those two are told
    apart by age. One nobody has written for BUSY_TIMEOUT is taken to be
    from a script that is no longer running, and one written before this
    Altium started cannot be from a script in it.

    A report written in the last POLL_SECONDS is being written, and is
    not opened -- see `run_script` for why that matters.
    """
    try:
        written = os.stat(report).st_mtime
    except OSError:
        return False
    age = time.time() - written
    if age < POLL_SECONDS:
        return True
    if read_sentinel(report).get("stage") == "end" or age >= BUSY_TIMEOUT:
        return False
    started = _altium_started()
    return started is not None and written >= started


def _wait_until_idle(
    exe: str, reports, timeout: float, log: Callable[[str], None],
) -> Optional[str]:
    """Wait for Altium to finish whatever it is already running.

    Returns None once it has, or a sentence saying why it has not.

    Asked in two halves, because neither is enough alone.

    First, the reports. A script given up on carries on inside Altium,
    and one launched behind it does not wait for it to end: it runs the
    moment the first lets a message through, nested inside it. Its own
    report is the only thing that says when it has really finished.

    Then Ping.pas, which does nothing: its stub exits only once Altium
    takes it, and Altium takes nothing while it is sitting on a dialog --
    or running a script that has no report yet, such as one that failed
    to parse. About a second when Altium is free.

    Every PROBE_RETRY seconds the ping's stub is withdrawn and a fresh one
    sent. A stub already waiting when a dialog in Altium was closed was
    seen to stay stuck for a minute and a half after it had gone -- and
    to hold up a second stub behind it -- while one sent after the dialog
    closed got through at once. A ping, being a no-op, is also the one
    request that costs nothing if a withdrawal comes too late to stop it.
    """
    from .. import paths

    started = time.monotonic()
    deadline = started + timeout
    said = False

    def waiting(what):
        nonlocal said
        if not said:
            log(f"{what} -- waiting up to {timeout:.0f}s before launching another.")
            said = True

    while True:
        running = [r for r in reports if _still_running(r)]
        if not running:
            break
        names = " and ".join(os.path.basename(r) for r in running)
        if time.monotonic() >= deadline:
            return (
                f"an earlier Altium script was still running after {timeout:.0f}s "
                f"({names} has not reached its end). Nothing was launched; if "
                "Altium is showing a dialog, it is waiting for you."
            )
        waiting(f"An earlier Altium script is still running ({names} has not reached its end)")
        time.sleep(POLL_SECONDS)

    # However long that took, the ping gets one full try.
    deadline = max(deadline, time.monotonic() + PROBE_RETRY)
    while True:
        probe = _launch(exe, str(paths.ALTIUM_PING), "Ping")
        retry_at = min(time.monotonic() + PROBE_RETRY, deadline)
        while probe.poll() is None and time.monotonic() < retry_at:
            if time.monotonic() - started > 5:
                waiting("Altium is not taking scripts yet")
            time.sleep(0.25)
        # A stub that exited by itself was taken; one that is not a stub
        # at all started a fresh Altium, which has nothing before it.
        if not _withdraw(probe):
            if said:
                log(f"Altium is free again after {time.monotonic() - started:.0f}s.")
            return None
        if time.monotonic() >= deadline:
            return (
                f"Altium would not take a script for {time.monotonic() - started:.0f}s "
                "-- most likely it is showing a dialog, which is waiting for you. "
                "Nothing was launched."
            )


@contextlib.contextmanager
def _one_at_a_time(log: Callable[[str], None]):
    """Hold Altium for one job -- see `_ALTIUM_IN_USE`."""
    if not _ALTIUM_IN_USE.acquire(blocking=False):
        log("Altium is drawing another run's layout -- waiting for it to finish.")
        _ALTIUM_IN_USE.acquire()
    try:
        yield
    finally:
        _ALTIUM_IN_USE.release()


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

    Nothing is launched until Altium has finished what it was already
    running. A script this gave up on is still running, and one launched
    behind it runs nested inside it, against the same parameter file,
    report and boards -- while this side reads the first one's report as
    its own. From the sweep that prompted it: a first combination failed
    with no output at all because the previous run's script was still
    going, and every launch after it piled up behind that one.
    """
    exe = find_altium_exe()
    if exe is None:
        raise RuntimeError(
            "Altium Designer not found -- looked for X2.EXE under "
            + " and ".join(_ALTIUM_ROOTS)
        )
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"Altium script not found: {script_path}")

    with _one_at_a_time(log):
        warm = altium_is_running()
        if warm:
            from .. import paths

            # Every script of ours, not just this one: an export launched
            # while an abandoned generator is still drawing reads a board
            # that is half one combination and half the next.
            reports = {sentinel_path} | {str(p) for p in (
                paths.ALTIUM_GENERATE_REPORT, paths.ALTIUM_COPPER_REPORT,
                paths.ALTIUM_MEASURE_REPORT,
            )}
            busy = _wait_until_idle(exe, sorted(reports), BUSY_TIMEOUT, log)
            if busy:
                log(busy)
                return False, {"error": busy}

        if not _clear_sentinel(sentinel_path):
            return False, {"error": (
                f"the previous script still has {os.path.basename(sentinel_path)} open. "
                "Altium is most likely sitting on a dialog -- look at its window."
            )}

        if timeout is None:
            timeout = WARM_TIMEOUT if warm else COLD_START_TIMEOUT

        log(f"Launching Altium: {procedure} (timeout {timeout:.0f}s)")
        launcher = _launch(exe, script_path, procedure)

        # The report is only opened once it has stopped changing, and this
        # is the reason why. The script rewrites it from scratch on every
        # progress line (`Say`), and Delphi's SaveToFile takes the file
        # EXCLUSIVELY: a handle held on this side at that moment does not
        # slow the script down, it fails its write -- and a failed write
        # inside Altium is a modal dialog waiting for a human who is not
        # there. So while the script is working the file is left alone
        # entirely, and what eventually gets read is a file nobody is
        # writing. It costs one poll interval to notice the end, which is
        # nothing next to what it avoids.
        #
        # It matters more than it used to: with Altium, SolidWorks and AEDT
        # all working at once, a read that used to take microseconds can sit
        # behind the other two's disk traffic for far longer.
        deadline = time.monotonic() + timeout
        written = None
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            fingerprint = _fingerprint(sentinel_path)
            settled = fingerprint is not None and fingerprint == written
            written = fingerprint
            if not settled:
                continue
            report = read_sentinel(sentinel_path)
            if report.get("stage") == "end":
                ok = report.get("ok", "1") == "1"
                log(f"Altium script finished ({'ok' if ok else 'reported a failure'}).")
                return ok, report

        # Only a script that never started can be taken back. One that
        # did carries on after this returns; the next launch waits for it.
        if _withdraw(launcher):
            reason = (
                f"Altium never started {procedure} -- it was busy with something "
                f"else for all {timeout:.0f}s, so the request was withdrawn."
            )
            log(reason)
            return False, {"error": reason}

        partial = read_sentinel(sentinel_path)
        if partial:
            log(
                "Altium script timed out. It got as far as: "
                + ", ".join(f"{k}={v}" for k, v in partial.items())
            )
        else:
            log("Altium script timed out having written nothing at all.")
        log("Altium is still running it; the next launch will wait for it to finish.")
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

    Adds the die's opening too -- its size, its depth, and whether it
    goes through. Decided here rather than in the DelphiScript so that
    the script only draws, and so that HFSS, which asks `cavity.plan`
    the same question, cannot get a different answer.
    """
    from . import cavity, cpw, smp

    out = dict(values)
    # The connector land pattern travels with the parameters so the
    # vendor's numbers live in one place rather than being retyped into
    # the DelphiScript.
    out.update(smp.as_parameters())
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
    out.update(cavity.as_parameters(cavity.plan(values)))
    return out


# --- measuring the connector footprint --------------------------------


def measure_footprint(
    footprint: Optional[str] = None,
    library: Optional[str] = None,
    out: Optional[str] = None,
    log: Callable[[str], None] = logger.info,
) -> Tuple[bool, str]:
    """Read the connector footprint out of its library and record it.

    Run this when the connector changes, not on every sweep: it opens
    Altium, and its output is a source file that is committed. `design.smp`
    reads that file and derives everything the board around the launch is
    drawn from -- the envelope, the anti-pad, the escape slot.

    It exists because a PcbLib is a compiled compound document that only
    Altium can open. The alternative was reading numbers off a vendor
    drawing and retyping them, which is how this project ended up with a
    launch missing its anti-pad the first time.

    `footprint`, `library` and `out` default to the ones `design.smp`
    and `paths` name, which is what you want for a re-measure. Give
    `out` to size up a candidate connector without overwriting the one
    the boards are currently built from.

    Bootstrapping a connector that has never been measured means passing
    `footprint` explicitly: `design.smp` cannot be imported at all until
    the file this produces exists, which is the price of it having no
    second copy of the numbers to fall back on.
    """
    from .. import paths

    source = library or str(paths.ALTIUM_SMP_LIBRARY)
    if footprint is None:
        from . import smp

        footprint = smp.FOOTPRINT
    target = footprint
    if not os.path.isfile(source):
        return False, f"no connector library at {source}"

    with _one_at_a_time(log):
        # The measurer reads its inputs from the same parameter file as
        # everything else, so there is one wire format across this boundary
        # rather than a second one for this script alone.
        write_params(
            str(paths.ALTIUM_PARAMS_FILE), {},
            extra={
                "smp_library": source,
                "smp_footprint": target,
                "smp_measured": out or str(paths.ALTIUM_SMP_FOOTPRINT_FILE),
            },
        )

        log(f"Measuring {target} in {os.path.basename(source)}...")
        ok, report = run_script(
            str(paths.ALTIUM_FOOTPRINT_MEASURER), "MeasureFootprint",
            str(paths.ALTIUM_MEASURE_REPORT), log=log,
        )
        if not ok:
            return False, _describe(report, "Altium footprint measurement")

    log(
        f"Measured {report.get('pads', '?')} pads, "
        f"{report.get('copper_regions', '?')} copper shapes and "
        f"{report.get('bodies', '?')} 3D bodies into "
        f"{out or paths.ALTIUM_SMP_FOOTPRINT_FILE}."
    )
    return True, ""


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
    from . import constraints
    from .. import paths

    # Cheaper to refuse here than to let Altium build a board that is
    # quietly wrong -- and the message names the parameter to change.
    reason = constraints.check_layout(values)
    if reason:
        return False, f"layout cannot be built: {reason}"

    materials = materials or {}
    params = build_params(
        values,
        mat_pcb=materials.get("mat_pcb", "Rogers TMM 10i (tm)"),
        mat_chip=materials.get("mat_chip", "silicon"),
    )
    from . import smp

    # Held from the parameter file to the copy out of generated/:
    # every file in between is shared with whichever job runs next.
    with _one_at_a_time(log):
        write_params(
            str(paths.ALTIUM_PARAMS_FILE), params,
            extra={
                "out_dir": str(paths.ALTIUM_OUT_DIR),
                # The launch is placed from the vendor library rather than
                # drawn, so the generator needs to be told where that library
                # is and which of its nine footprints this part is.
                "smp_library": str(paths.ALTIUM_SMP_LIBRARY),
                "smp_footprint": smp.FOOTPRINT,
            },
        )

        # The bond layout is computed once and shared: Altium places the pads
        # from it and HFSS builds the wires from it, so the two cannot drift
        # apart when a parameter changes.
        from . import bonds as bond_layout

        os.makedirs(paths.ALTIUM_OUT_DIR, exist_ok=True)
        bond_layout.write_file(
            str(paths.ALTIUM_BONDS_FILE),
            bond_layout.layout(values, params["pcb_cpw_gap"]),
        )

        ok, report = run_script(
            str(paths.ALTIUM_GENERATOR), "GenerateFromParams",
            str(paths.ALTIUM_GENERATE_REPORT), log=log,
        )
        if not ok:
            return False, _describe(report, "Altium layout generation")

        incomplete = _missing_features(report)
        if incomplete:
            return False, f"Altium built an incomplete board: {incomplete}"

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
        for name, path in (("copper.txt", source), ("bonds.txt", paths.ALTIUM_BONDS_FILE)):
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as src:
                content = src.read()
            with open(os.path.join(combo_dir, name), "w", encoding="utf-8") as dst:
                dst.write(content)
        destination = os.path.join(combo_dir, "copper.txt")

        # The copper belongs to this combination, so it is kept with it
        # rather than left in the shared output folder to be overwritten by
        # the next combination.
        log(f"Copper for this combination written to {destination}")
        return True, ""


def _missing_features(report: Dict[str, str]) -> str:
    """What the generator said it could not build, or "" if it built it all.

    `run_script` only answers "did the script reach its end", and the
    generator is written to reach its end whatever happens -- every
    feature is guarded separately precisely so that one failure does not
    take the rest of the board with it. That is the right behaviour for
    the script and the wrong place to stop reading.

    Written after a run where the parameter file, produced by a stale
    process, named no connector library. The generator said so --
    `smp_library=NOT_CONFIGURED`, `package_ports=NO_LAUNCH_PLACED` --
    saved a board with no launch on it, and the sweep carried that board
    forward without a word. It only surfaced two stages later, as a
    parse error with nothing to do with connectors.

    So: a board missing a feature is a failed combination, recorded and
    skipped like any other, rather than a model of something nobody
    meant to simulate.
    """
    problems = []

    if report.get("package_ports") == "NO_LAUNCH_PLACED":
        why = report.get("smp_library") or report.get("smp_footprint") or "unknown"
        problems.append(f"no connector was placed ({why})")

    for key, feature in (
        ("package_body", "the package's copper"),
        ("chip_body", "the die's copper"),
        ("package_cavity", "the die's opening"),
        ("package_bond_pads", "the board's bond fingers"),
        ("chip_bond_pads", "the die's bond pads"),
    ):
        value = report.get(key)
        if value is None:
            problems.append(f"{feature} was never reported on")
        elif value in ("FAIL", "0"):
            problems.append(f"{feature} failed")

    # The cavity is only a cutout if Altium agreed it was one. A Kind
    # that did not take leaves a plain copper region on the keep-out
    # layer, which looks like a cutout in the tree and is not one.
    if report.get("cavity_kind_readback") in ("WRONG", "unreadable"):
        problems.append("the die's opening is not the kind of region it was set to")

    return "; ".join(problems)


def _describe(report: Dict[str, str], what: str) -> str:
    """Turn a partial sentinel into a sentence worth reading on the run
    page. How far the script got is the only diagnostic Altium gives."""
    if not report:
        return f"{what} produced no output at all -- Altium may be showing a modal dialog."
    if report.get("error"):
        return f"{what} failed: {report['error']}"
    reached = ", ".join(f"{k}={v}" for k, v in report.items())
    return f"{what} did not finish. It got as far as: {reached}"
