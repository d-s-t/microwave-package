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
    ok, reason = step_generation.save_as_step(model, destination, options=SAVE_SILENT | SAVE_COPY)
    if not ok:
        raise RuntimeError(f"could not save {os.path.basename(destination)}: {reason}")


def export_part(
    part: str,
    equations_path: str,
    outputs: Dict[str, str],
    title: str,
    values: Dict[str, float],
    log: Callable[[str], None] = logger.info,
) -> None:
    """Make the requested files for one part.

    `outputs` maps "sldprt" and/or "drawing" to the path to write. Raises
    RuntimeError, saying what, when a file cannot be made.
    """
    base = str(paths.SOLIDWORKS_PART_FILES[part])
    if not os.path.isfile(base):
        raise RuntimeError(f"base part not found: {base}")
    if not os.path.isfile(equations_path):
        raise RuntimeError(f"this combination's equations file is missing: {equations_path}")

    app, err = step_generation.connect_solidworks()
    if not app:
        raise RuntimeError(f"could not connect to SolidWorks ({err})")
    if _open_title(app, base):
        raise RuntimeError(
            f"{os.path.basename(base)} is open in SolidWorks (a run may be using it) -- "
            "close it there, or wait for the run to finish, and try again.")

    opened = []
    try:
        model, err = step_generation.open_and_activate_part(app, base)
        if not model:
            raise RuntimeError(f"could not open {base}: {err}")
        opened.append(base)
        log(f"{part}: applying this combination's equations")
        step_generation.link_equations(model, equations_path, log=log)
        # link_equations repoints only when the link differs; a part that
        # already pointed here still needs to re-read the file.
        try:
            model.GetEquationMgr.UpdateValuesFromExternalEquationFile
        except Exception:
            pass
        step_generation.rebuild_model(model)

        if "sldprt" in outputs:
            log(f"{part}: saving the SolidWorks part")
            save_part_copy(model, outputs["sldprt"])
        if "drawing" in outputs:
            from . import solidworks_drawing

            log(f"{part}: drawing and exporting the PDF")
            solidworks_drawing.make_pdf(app, model, base, part, outputs["drawing"], title, values, log)
    finally:
        for path in opened:
            try:
                app.CloseDoc(os.path.basename(path))
            except Exception as exc:
                logger.warning("Could not close %s: %s", path, exc)
