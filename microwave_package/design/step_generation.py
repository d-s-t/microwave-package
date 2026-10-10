"""
SolidWorks STEP-file generation: for each parameter combination in a
sweep, edits the shared equations file, rebuilds box.SLDPRT and
lid_inside.SLDPRT, and exports each straight to its own STEP file. Runs
on the machine that has SolidWorks installed (requires `pywin32`).

There is no assembly and no mates here any more. box and lid_inside sit
in the same box only once they reach AEDT -- model_builder.place_lid_inside
positions lid_inside there by measuring its imported geometry against the
board, the same way place_connectors already does for the SMP connector.
The two parts are still linked to the same external equations file
(`base_parts/default.txt`, confirmed via SolidWorks' own Equation Manager
for both), so writing it once and rebuilding each part independently
keeps them in step without an assembly to hold them together.

`Session` is the whole of it -- one connection, both parts open at once,
one combination at a time. Which combinations it is given, and in what
order, is decided by `workflow/stages.py`, which runs this alongside the
Altium and AEDT stages rather than before them.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, Optional, Tuple

from . import equations as eq

logger = logging.getLogger("microwave_package")


def connect_solidworks() -> Tuple[Optional[object], Optional[str]]:
    """Returns (swApp, None) on success, or (None, reason) on failure."""
    import win32com.client

    try:
        logger.info("Connecting to SolidWorks...")
        swApp = win32com.client.Dispatch("SldWorks.Application")
        swApp.Visible = True
        swApp.SetUserPreferenceIntegerValue(278, 214)  # STEP export -> AP214
        return swApp, None
    except Exception as e:
        logger.error("Failed to connect to SolidWorks: %s", e)
        return None, str(e)


def open_and_activate_part(swApp, part_path: str) -> Tuple[Optional[object], Optional[str]]:
    """Returns (model, None) on success, or (None, reason) on failure.

    Opens `part_path` as a plain part document (swDocPART = 1) -- there is
    no assembly document in this pipeline any more, just box.SLDPRT and
    lid_inside.SLDPRT opened side by side.

    Both OpenDoc6 and ActivateDoc3 report a numeric error/warning code via
    their ByRef out-params -- SolidWorks' own explanation for why the
    call didn't do what you asked (file already open elsewhere, a
    read-only/rebuild/reference-not-found dialog waiting for a click on
    the -- normally hidden -- SolidWorks window since this runs with
    Visible=True, a future-version file, etc.). Each call needs its OWN
    VARIANT for this -- reusing one between calls means the second call's
    result overwrites the first's before anyone reads it, and no caller
    here ever read either, so a failure just came back as an unexplained
    None. This surfaces both codes instead of throwing them away.
    """
    import pythoncom
    import win32com.client

    SW_DOC_PART = 1

    try:
        logger.info("Opening part: %s", part_path)
        open_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        open_warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        swApp.OpenDoc6(part_path, SW_DOC_PART, 1, "", open_err, open_warn)
        if open_err.value:
            logger.warning(
                "OpenDoc6 reported error code %s (warning code %s) for %s -- see SolidWorks' "
                "swFileLoadError_e/swFileLoadWarning_e enums for what the code means.",
                open_err.value, open_warn.value, part_path,
            )

        activate_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        swModel = swApp.ActivateDoc3(part_path, False, SW_DOC_PART, activate_err)
        if not swModel:
            reason = (
                f"OpenDoc6 error={open_err.value}, warning={open_warn.value}; "
                f"ActivateDoc3 error={activate_err.value}. If OpenDoc6's error is 0 but this still "
                "failed, check the SolidWorks window itself (it's running Visible=True) for a "
                "blocking dialog -- e.g. \"open read-only?\", a missing-reference prompt, or a "
                "rebuild-on-open warning -- headless automation can't click through those."
            )
            logger.error("Failed to activate part %s: %s", part_path, reason)
            return None, reason
        return swModel, None
    except Exception as e:
        logger.error("Error opening/activating part %s: %s", part_path, e)
        return None, str(e)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def link_equations(swModel, equations_path: str, log: Callable[[str], None] = logger.info) -> bool:
    """Make sure the part reads its dimensions from `equations_path`.

    A SolidWorks part links its equations to an external file by ABSOLUTE
    path, and stores that path in the part. A part saved in one checkout
    or worktree therefore reads the equations file of the checkout it was
    saved in, whatever file this pipeline writes: the box and lid came out
    with the screws and floor of someone else's `default.txt`, so a run
    that asked for no screws had them, and its board sat inside a floor
    that had not moved with it.

    So the link is pointed at the file the pipeline is writing, and the
    values re-read, whenever it points anywhere else. Returns whether it
    had to be changed; a part with no link, or one whose link cannot be
    read, is left alone (and said so).
    """
    try:
        manager = swModel.GetEquationMgr
        linked = getattr(manager, "FilePath", None)
    except Exception as exc:
        log(f"WARNING: could not read the equation link of a part ({exc}); its dimensions may "
            "not follow this run's equations file.")
        return False
    if not linked or _same_path(str(linked), equations_path):
        return False
    try:
        manager.FilePath = str(equations_path)
        manager.UpdateValuesFromExternalEquationFile
        swModel.ForceRebuild3(False)
    except Exception as exc:
        log(f"WARNING: could not point a part's equations at {equations_path} ({exc}).")
        return False
    log(f"A part was linked to {linked}; it now reads {equations_path}.")
    return True


def rebuild_model(swModel) -> None:
    logger.info("Rebuilding part...")
    try:
        swModel.ForceRebuild3(False)
    except Exception as e:
        logger.warning("Rebuild failed: %s", e)


def save_as_step(swModel, step_filepath: str, options: int = 1) -> Tuple[bool, Optional[str]]:
    """
    Save the active document as a STEP file.

    Under plain (late-bound) Dispatch, SolidWorks' SaveAs return value and
    its ByRef Errors/Warnings out-params do not marshal back to Python
    reliably (a known win32com/SolidWorks quirk) -- the file is often
    written correctly even when Python sees `success == False`.

    Rather than trust the return value, we check the file itself: delete
    any pre-existing file with the same name first, call SaveAs, then
    confirm the new file actually exists (and is non-empty).

    `options` is swSaveAsOptions_e: 1 silent (the default), and 2 saves a
    copy, which is how `solidworks_manufacturing` writes an SLDPRT
    without the open document taking the new name. Despite the name this
    writes whatever the file's extension says.
    """
    import pythoncom
    import win32com.client

    if os.path.exists(step_filepath):
        try:
            os.remove(step_filepath)
        except OSError as e:
            return False, f"Could not remove existing file before save: {e}"

    try:
        # The 4th arg (ExportData) is typed as a COM object pointer, and
        # the 5th/6th args (Errors, Warnings) are ByRef longs. Plain
        # Python None/0 don't auto-convert to these under late-bound
        # dispatch and raise "Type mismatch" -- each must be wrapped
        # explicitly as the right VARIANT type.
        export_data = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        err_var = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warn_var = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        swModel.Extension.SaveAs(step_filepath, 0, options, export_data, err_var, warn_var)
    except Exception as e:
        return False, f"SaveAs raised an exception: {e}"

    for _ in range(20):
        if os.path.exists(step_filepath) and os.path.getsize(step_filepath) > 0:
            return True, None
        time.sleep(0.25)

    return False, "SaveAs returned but no (non-empty) STEP file was found on disk"


def write_common_dimensions(run, log: Callable[[str], None] = logger.info) -> Optional[str]:
    """
    Write `common_dimensions.txt` in the run folder: the equation lines
    that are identical across every combination in the run.

    Rather than reasoning about which parameters were swept, this reads
    back the per-combo `dimensions.txt` snapshots and keeps the lines they
    all agree on -- so whatever varied (directly or through a formula that
    depends on it) drops out, and what is left is exactly the geometry
    this run held constant.
    """
    snapshots = []
    for combo_id, combo in run.state["combos"].items():
        path = os.path.join(run.combo_dir(combo_id), "dimensions.txt")
        if os.path.isfile(path):
            snapshots.append(eq.read_file(path).splitlines())
    if not snapshots:
        return None

    shared = set(snapshots[0])
    for lines in snapshots[1:]:
        shared &= set(lines)

    # keep the first snapshot's ordering, and each line only once
    seen = set()
    common_lines = []
    for line in snapshots[0]:
        if line in shared and line not in seen:
            seen.add(line)
            common_lines.append(line)

    out_path = os.path.join(run.run_dir, "common_dimensions.txt")
    eq.write_file(out_path, "\n".join(common_lines) + "\n")
    log(f"Wrote {out_path} ({len(common_lines)} line(s) shared by all {len(snapshots)} combination(s)).")
    return out_path


class Session:
    """box.SLDPRT and lid_inside.SLDPRT, both held open across a run's
    combinations.

    SolidWorks drives its documents one at a time on a single shared
    equations file, so a run's combinations go through it strictly in
    order -- which is why this is a session opened once and reused rather
    than a function that connects per combination. What it costs to open
    (and what a combination costs to rebuild) is exactly why the Altium
    and AEDT stages no longer wait behind it: see `workflow/stages.py`.

    The original equations file is held from the moment the session
    opens and written back when it closes, so the shared file is left as
    it was found however the run ends.
    """

    def __init__(self, app, parts: Dict[str, object], equations_path: str,
                 original_content: str) -> None:
        self.app = app
        self.parts = parts
        self.equations_path = equations_path
        self.original_content = original_content

    @classmethod
    def open(cls, equations_path: str, part_paths: Dict[str, str],
             log: Callable[[str], None] = logger.info) -> "Session":
        """Connect to SolidWorks and open every part in `part_paths`.

        Raises RuntimeError if any of it fails: a run cannot produce
        geometry without this, and failing here rather than once per
        combination is what says so plainly.
        """
        original_content = eq.read_file(equations_path)
        app, err = connect_solidworks()
        if not app:
            raise RuntimeError(
                f"Could not connect to SolidWorks -- is it installed and licensed on this machine? ({err})"
            )
        parts: Dict[str, object] = {}
        for name, part_path in part_paths.items():
            model, err = open_and_activate_part(app, part_path)
            if not model:
                eq.write_file(equations_path, original_content)
                raise RuntimeError(f"Could not open/activate {name} ({part_path}): {err}")
            link_equations(model, equations_path, log=log)
            parts[name] = model
        return cls(app, parts, equations_path, original_content)

    def build(self, combo_id: str, content: str, combo_dir: str,
              log: Callable[[str], None] = logger.info) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """Rebuild every part for one combination and export each to its
        own STEP file.

        `content` is the whole equations file as this combination wants
        it -- worked out by the caller (see `equations.with_values`), not
        here, so the Altium stage can work out the same numbers for a
        different combination at the same time. Writing it once and
        rebuilding every open part off of it is what keeps box and
        lid_inside in step without an assembly holding them together.

        Returns ({name: step_path, ...}, None) on success, or (None, reason).
        """
        eq.write_file(self.equations_path, content)
        os.makedirs(combo_dir, exist_ok=True)

        step_paths: Dict[str, str] = {}
        for name, model in self.parts.items():
            rebuild_model(model)
            step_path = os.path.join(combo_dir, f"{combo_id}_{name}.STEP")
            success, err = save_as_step(model, step_path)
            if not success:
                return None, f"{name}: {err}"
            step_paths[name] = step_path

        # Snapshot of the exact equations used for this combo, so
        # later steps (min-frequency estimate) can read it back
        # without depending on the shared file's current state.
        eq.write_file(os.path.join(combo_dir, "dimensions.txt"), content)

        log(f"Saved {', '.join(step_paths.values())}")
        return step_paths, None

    def close(self, log: Callable[[str], None] = logger.info) -> None:
        """Put the shared equations file back the way it was found."""
        eq.write_file(self.equations_path, self.original_content)
        log("Restored original equations file.")
