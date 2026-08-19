"""
SolidWorks STEP-file generation: for each parameter combination in a
sweep, edits the shared equations file, rebuilds the assembly, and
exports a STEP file. Runs on the machine that has SolidWorks installed
(requires `pywin32`).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, Optional, Tuple

from . import equations as eq

logger = logging.getLogger("eigen_mode_analyze")


def connect_solidworks():
    import win32com.client

    try:
        logger.info("Connecting to SolidWorks...")
        swApp = win32com.client.Dispatch("SldWorks.Application")
        swApp.Visible = True
        swApp.SetUserPreferenceIntegerValue(278, 214)  # STEP export -> AP214
        return swApp
    except Exception as e:
        logger.error("Failed to connect to SolidWorks: %s", e)
        return None


def open_and_activate_assembly(swApp, assembly_path: str):
    import pythoncom
    import win32com.client

    try:
        logger.info("Opening assembly: %s", assembly_path)
        arg_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        arg_warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        swApp.OpenDoc6(assembly_path, 2, 1, "", arg_err, arg_warn)
        logging_model = swApp.ActivateDoc3(assembly_path, False, 2, arg_err)
        if not logging_model:
            logger.error("Failed to activate assembly: %s", assembly_path)
            return None
        return logging_model
    except Exception as e:
        logger.error("Error opening/activating assembly: %s", e)
        return None


def rebuild_model(swModel) -> None:
    logger.info("Rebuilding assembly...")
    try:
        swModel.ForceRebuild3(False)
    except Exception as e:
        logger.warning("Rebuild failed: %s", e)


def save_as_step(swModel, step_filepath: str) -> Tuple[bool, Optional[str]]:
    """
    Save the active document as a STEP file.

    Under plain (late-bound) Dispatch, SolidWorks' SaveAs return value and
    its ByRef Errors/Warnings out-params do not marshal back to Python
    reliably (a known win32com/SolidWorks quirk) -- the file is often
    written correctly even when Python sees `success == False`.

    Rather than trust the return value, we check the file itself: delete
    any pre-existing file with the same name first, call SaveAs, then
    confirm the new file actually exists (and is non-empty).
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
        swModel.Extension.SaveAs(step_filepath, 0, 1, export_data, err_var, warn_var)
    except Exception as e:
        return False, f"SaveAs raised an exception: {e}"

    for _ in range(20):
        if os.path.exists(step_filepath) and os.path.getsize(step_filepath) > 0:
            return True, None
        time.sleep(0.25)

    return False, "SaveAs returned but no (non-empty) STEP file was found on disk"


def generate_step_files(
    combos: Dict[str, Dict[str, float]],
    equations_path: str,
    assembly_path: str,
    run,
    log: Callable[[str], None] = logger.info,
) -> None:
    """
    Build a STEP file for every combo, skipping ones already generated
    (resume support -- checked via `run.state`). Restores the equations
    file to its original content when done, even on failure.
    """
    pending = {
        combo_id: params
        for combo_id, params in combos.items()
        if run.state["combos"][combo_id].get("step_status") != "done"
    }
    if not pending:
        log("All STEP files already generated -- skipping SolidWorks step.")
        return

    original_content = eq.read_file(equations_path)
    swApp = connect_solidworks()
    if not swApp:
        raise RuntimeError("Could not connect to SolidWorks -- is it installed and licensed on this machine?")

    swModel = open_and_activate_assembly(swApp, assembly_path)
    if not swModel:
        eq.write_file(equations_path, original_content)
        raise RuntimeError(f"Could not open/activate assembly: {assembly_path}")

    try:
        for combo_id, params in pending.items():
            log(f"Building geometry for {combo_id} ({params})...")
            content = original_content
            for name, value in params.items():
                content = eq.set_parameter(content, name, value)
            eq.write_file(equations_path, content)
            rebuild_model(swModel)

            combo_dir = run.combo_dir(combo_id)
            os.makedirs(combo_dir, exist_ok=True)
            # Snapshot of the exact equations used for this combo, so
            # later steps (min-frequency estimate) can read it back
            # without depending on the shared file's current state.
            eq.write_file(os.path.join(combo_dir, "dimensions.txt"), content)

            step_path = os.path.join(combo_dir, f"{combo_id}.STEP")
            success, err = save_as_step(swModel, step_path)
            if success:
                log(f"Saved {step_path}")
                run.update_combo(combo_id, step_path=step_path, step_status="done", step_error=None)
            else:
                log(f"FAILED to save STEP for {combo_id}: {err}")
                run.update_combo(combo_id, step_status="failed", step_error=err)
    finally:
        eq.write_file(equations_path, original_content)
        log("Restored original equations file.")
