"""
SolidWorks side of the manufacturing files: for one combination, an
SLDPRT of the box or lid with that combination's equations applied, and a
dimensioned drawing of it as a PDF.

Neither touches `base_parts/*.SLDPRT` on disk. The base part is opened,
pointed at the combination's own `dimensions.txt` (the exact equations
file the run built it from, kept in the combination's folder), rebuilt,
written out as a COPY into the combination's folder, and closed without
saving. The equations file is the combination's snapshot rather than the
shared `default.txt`, which a running sweep rewrites -- so a part saved
here keeps reading the right numbers no matter what the shared file holds
afterwards.

Needs SolidWorks and pywin32; the caller (`workflow.manufacturing`) holds
the lock that keeps this and the sweep from using it at once.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, Optional

from .. import paths
from . import step_generation

logger = logging.getLogger("microwave_package")

SAVE_SILENT = 1
SAVE_COPY = 2
MAX_PATH = 260


def _open_title(app, path: str) -> bool:
    """Whether SolidWorks already has `path` open. A part that is would be
    changed under whoever is using it, and two same-named documents cannot
    be open at once anyway."""
    try:
        return app.GetOpenDocumentByName(path) is not None
    except Exception:
        return False


def save_part_copy(model, destination: str) -> None:
    """Write `model` as an SLDPRT at `destination` without renaming the
    open document. Raises RuntimeError if no file came out."""
    if len(destination) >= MAX_PATH:
        raise RuntimeError(
            f"the path is {len(destination)} characters, too long for SolidWorks to save "
            f"({MAX_PATH - 1} at most): keep the runs folder shorter (MICROWAVE_RUNS_PATH).")
    ok, reason = step_generation.save_as_step(model, destination, options=SAVE_SILENT | SAVE_COPY)
    if not ok:
        raise RuntimeError(f"could not save {os.path.basename(destination)}: {reason}")


def _make_copy(app, part: str, base: str, equations_path: str, destination: str,
               log: Callable[[str], None]) -> None:
    """Open the base part, apply the combination's equations, save a copy
    that no longer depends on the equations file, and close the base
    unsaved."""
    if _open_title(app, base):
        raise RuntimeError(
            f"{os.path.basename(base)} is open in SolidWorks (a run may be using it) -- "
            "close it there, or wait for the run to finish, and try again.")
    try:
        model, err = step_generation.open_and_activate_part(app, base)
        if not model:
            raise RuntimeError(f"could not open {base}: {err}")
        log(f"{part}: applying this combination's equations")
        step_generation.link_equations(model, equations_path, log=log)
        # link_equations repoints only when the link differs; a part that
        # already pointed here still needs to re-read the file.
        try:
            model.GetEquationMgr.UpdateValuesFromExternalEquationFile
        except Exception:
            pass
        step_generation.rebuild_model(model)
        # Cut the link to the equations file before copying: the values
        # stay in the part as ordinary equations, so the file opens
        # anywhere without hunting for a text file on this machine.
        try:
            model.GetEquationMgr.LinkToFile = False
        except Exception as exc:
            log(f"WARNING: could not unlink the equations ({exc}); the part will ask for its file.")
        log(f"{part}: saving the SolidWorks part")
        save_part_copy(model, destination)
    finally:
        try:
            app.CloseDoc(os.path.basename(base))
        except Exception as exc:
            logger.warning("Could not close %s: %s", base, exc)


def _draw_copy(app, part: str, sldprt_path: str, pdf_path: str, slddrw_path: str, title: str,
               values: Dict[str, float], material: str, log: Callable[[str], None]) -> None:
    """Draw the saved copy (not the base part: it holds this combination's
    dimensions, and its name is unique): from the saved drawing template
    when there is one that can follow this combination, from scratch when
    there is not. Either way the PDF is written and the drawing is kept
    beside the part copy as an SLDDRW."""
    from . import solidworks_drawing, solidworks_template
    import pythoncom
    import win32com.client

    if _open_title(app, sldprt_path):
        raise RuntimeError(f"{os.path.basename(sldprt_path)} is open in SolidWorks -- close it and try again.")
    errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    model = app.OpenDoc6(sldprt_path, 1, 1, "", errors, warnings)
    if model is None:
        raise RuntimeError(f"could not open {sldprt_path} (SolidWorks error {errors.value})")
    try:
        # measured before any drawing is open (see layout_for_part)
        layout = solidworks_drawing.layout_for_part(model, part, values, log)
        left_off = None
        if solidworks_template.has_template(part):
            log(f"{part}: drawing from the saved template")
            left_off = solidworks_template.fill_template(
                app, part, sldprt_path, slddrw_path, pdf_path, title, values, material, layout, log)
        if left_off is None:
            log(f"{part}: drawing and exporting the PDF")
            drawing, sheet, _ = solidworks_drawing.build_drawing(
                app, model, sldprt_path, part, title, values, log, material, layout=layout)
            ok, reason = step_generation.save_as_step(drawing, slddrw_path, SAVE_SILENT)
            if not ok:
                log(f"WARNING: could not keep the drawing as {os.path.basename(slddrw_path)}: {reason}")
            solidworks_drawing.finish_pdf(app, drawing, sheet, pdf_path)
            left_off = sheet.rejected
        if left_off:
            log(f"WARNING: {len(left_off)} dimension(s) were left off the drawing: " + "; ".join(left_off))
    finally:
        try:
            app.CloseDoc(os.path.basename(sldprt_path))
        except Exception as exc:
            logger.warning("Could not close %s: %s", sldprt_path, exc)


def export_part(
    part: str,
    equations_path: str,
    outputs: Dict[str, str],
    title: str,
    values: Dict[str, float],
    log: Callable[[str], None] = logger.info,
    sldprt_path: Optional[str] = None,
    material: str = "",
) -> None:
    """Make the requested files for one part.

    `outputs` maps "sldprt", "drawing" (the PDF) and "slddrw" (the drawing
    itself, kept to be opened and tuned) to the path to write. The
    drawing is made from the SLDPRT copy, which is `outputs["sldprt"]`, or
    `sldprt_path` when that already exists. Raises RuntimeError, saying
    what, when a file cannot be made.
    """
    base = str(paths.SOLIDWORKS_PART_FILES[part])
    if "sldprt" in outputs:
        sldprt_path = outputs["sldprt"]
    if not os.path.isfile(base):
        raise RuntimeError(f"base part not found: {base}")
    if not os.path.isfile(equations_path):
        raise RuntimeError(f"this combination's equations file is missing: {equations_path}")
    if "drawing" in outputs and (not sldprt_path or ("sldprt" not in outputs and not os.path.isfile(sldprt_path))):
        raise RuntimeError("a drawing needs the SLDPRT copy of the part")

    app, err = step_generation.connect_solidworks()
    if not app:
        raise RuntimeError(f"could not connect to SolidWorks ({err})")
    if "sldprt" in outputs:
        _make_copy(app, part, base, equations_path, outputs["sldprt"], log)
    if "drawing" in outputs:
        slddrw = outputs.get("slddrw") or os.path.splitext(outputs["drawing"])[0] + ".SLDDRW"
        _draw_copy(app, part, sldprt_path, outputs["drawing"], slddrw, title, values, material, log)
