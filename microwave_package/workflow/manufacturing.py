"""
Manufacturing files for one combination of a run: what a machine shop
and a PCB fab need to make the design, listed, generated on demand, and
served by the web app's "Manufacturing files" panel.

Three groups, each made by one producer (a "generate" in the panel):

    box     the box STEP (already made by the run), an SLDPRT of the box
            with this combination's equations applied, and a dimensioned
            drawing PDF
    lid     the same for lid_inside
    pcb     the Altium project (PcbDoc + PrjPcb), Gerber RS-274X, Excellon
            drill (plated / non-plated), ODB++, IPC-2581, a STEP of the
            board, and a fabrication drawing PDF -- the board only, never
            the chip (it is a stand-in for a part that is bought, not made)

The files land in `<combo folder>/manufacturing/`, named after the run
and combination so a download is self-describing. This module is the
bookkeeping: the catalog, what exists, who is generating what, and the
zip/project text. The parts that need SolidWorks or Altium live in
`design/solidworks_manufacturing.py` and `design/altium_generation.py`
and are imported only when a producer runs, so listing a combination's
files (and the tests of it) never touches either program.
"""
from __future__ import annotations

import datetime as _dt
import glob
import logging
import os
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..design import equations as eq

logger = logging.getLogger("microwave_package")

FOLDER = "manufacturing"
GROUPS = ("box", "lid", "pcb")
GROUP_TITLES = {"box": "Box", "lid": "Lid", "pcb": "PCB (board only, no chip)"}

# The part each CAD group is made from (keys of paths.SOLIDWORKS_PART_FILES).
PART_OF_GROUP = {"box": "box", "lid": "lid_inside"}


@dataclass(frozen=True)
class Spec:
    key: str
    group: str
    label: str
    description: str
    suffix: str            # file name is <run>__<combo><suffix>
    mime: str


_OCTET = "application/octet-stream"

CATALOG: List[Spec] = [
    Spec("box_step", "box", "Box - STEP", "3D solid, neutral CAD exchange format (AP214).",
         "_box.STEP", "model/step"),
    Spec("box_sldprt", "box", "Box - SolidWorks part",
         "Native SolidWorks part with this combination's dimensions applied.", "_box.SLDPRT", _OCTET),
    Spec("box_drawing", "box", "Box - dimensioned drawing (PDF)",
         "Views, sections, hole callouts and overall dimensions for the machine shop.",
         "_box_drawing.pdf", "application/pdf"),
    Spec("lid_step", "lid", "Lid - STEP", "3D solid, neutral CAD exchange format (AP214).",
         "_lid_inside.STEP", "model/step"),
    Spec("lid_sldprt", "lid", "Lid - SolidWorks part",
         "Native SolidWorks part with this combination's dimensions applied.", "_lid_inside.SLDPRT", _OCTET),
    Spec("lid_drawing", "lid", "Lid - dimensioned drawing (PDF)",
         "Views, sections, countersinks and overall dimensions for the machine shop.",
         "_lid_inside_drawing.pdf", "application/pdf"),
    Spec("pcb_project", "pcb", "Altium project (zip)",
         "Native .PcbDoc and .PrjPcb, ready to open in Altium Designer.", "_pcb_altium.zip", "application/zip"),
    Spec("pcb_gerber", "pcb", "Gerber RS-274X (zip)",
         "Top and bottom copper and board outline (4.6 metric).", "_pcb_gerber.zip", "application/zip"),
    Spec("pcb_drill", "pcb", "Excellon NC drill (zip)",
         "Plated holes (vias) and non-plated holes (screw clearance), separate files.",
         "_pcb_drill.zip", "application/zip"),
    Spec("pcb_odb", "pcb", "ODB++ (zip)", "Complete fabrication database for CAM, exported from Altium.",
         "_pcb_odb.zip", "application/zip"),
    Spec("pcb_ipc2581", "pcb", "IPC-2581 (xml)", "Single-file fabrication format, exported from Altium.",
         "_pcb_ipc2581.xml", "application/xml"),
    Spec("pcb_fab_drawing", "pcb", "Fabrication drawing (PDF)",
         "Outline, hole and drill tables, layer stack, finish, and minimum trace/gap.",
         "_pcb_fab_drawing.pdf", "application/pdf"),
    Spec("pcb_step", "pcb", "Board - STEP", "3D model of the bare board, exported from Altium.",
         "_pcb.STEP", "model/step"),
]

SPECS: Dict[str, Spec] = {spec.key: spec for spec in CATALOG}

# Files the run itself already produces, outside the manufacturing folder.
_RUN_STEP_PART = {"box_step": "box", "lid_step": "lid_inside"}


def output_dir(run, combo_id: str) -> str:
    """The folder a combination's manufacturing files go in (not created)."""
    return os.path.join(run.combo_path(combo_id), FOLDER)


def file_stem(run, combo_id: str) -> str:
    return f"{run.run_id}__{combo_id}"


def path_of(run, combo_id: str, key: str) -> Optional[str]:
    """Where a file lives if it exists, else None. KeyError for an
    unknown key."""
    spec = SPECS[key]
    part = _RUN_STEP_PART.get(key)
    if part is not None:
        # The run's own STEP export, kept where the pipeline put it.
        recorded = (run.state.get("combos", {}).get(combo_id, {}).get("step_paths") or {}).get(part)
        if recorded and os.path.isfile(recorded):
            return recorded
        found = sorted(glob.glob(os.path.join(run.combo_path(combo_id), f"*{spec.suffix}")))
        return found[0] if found else None
    candidate = os.path.join(output_dir(run, combo_id), file_stem(run, combo_id) + spec.suffix)
    return candidate if os.path.isfile(candidate) else None


def target_path(run, combo_id: str, key: str) -> str:
    """Where a file is to be written (the folder is created)."""
    folder = output_dir(run, combo_id)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, file_stem(run, combo_id) + SPECS[key].suffix)


def download_name(run, combo_id: str, key: str) -> str:
    return file_stem(run, combo_id) + SPECS[key].suffix


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


# -- who is generating what ---------------------------------------------

class Jobs:
    """In-process record of generation jobs, keyed by (run, combo, group).

    Held in memory on purpose: a job that was running when the server
    stopped is not running any more, and the files it did finish are
    on disk, which is what the listing shows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[tuple, dict] = {}

    def get(self, run_id: str, combo_id: str, group: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get((run_id, combo_id, group))
            return dict(job) if job else None

    def any_running(self) -> bool:
        with self._lock:
            return any(job["state"] == "running" for job in self._jobs.values())

    def start(self, run_id: str, combo_id: str, group: str,
              work: Callable[[Callable[[str], None]], None]) -> bool:
        """Run `work(log)` on a thread. False if this job is already running."""
        key = (run_id, combo_id, group)
        with self._lock:
            current = self._jobs.get(key)
            if current and current["state"] == "running":
                return False
            job = {"state": "running", "message": "starting", "error": None,
                   "started": time.time(), "finished": None}
            self._jobs[key] = job

        def log(message: str) -> None:
            with self._lock:
                job["message"] = message
            logger.info("manufacturing %s/%s/%s: %s", run_id, combo_id, group, message)

        def target() -> None:
            try:
                work(log)
                with self._lock:
                    job["state"] = "done"
                    job["message"] = "finished"
            except Exception as exc:  # reported on the panel, not raised into a dead thread
                logger.exception("manufacturing job %s failed", key)
                with self._lock:
                    job["state"] = "error"
                    job["error"] = str(exc) or exc.__class__.__name__
            finally:
                with self._lock:
                    job["finished"] = time.time()

        threading.Thread(target=target, daemon=True, name="manufacturing-" + "-".join(key)).start()
        return True


JOBS = Jobs()
# One SolidWorks / Altium job at a time, whichever run asked.
APP_LOCK = threading.Lock()


# -- the listing the panel shows -----------------------------------------

def listing(run, combo_id: str, jobs: Optional[Jobs] = None) -> List[dict]:
    """The panel's content: groups, each with its files and state.

    Pure bookkeeping over what is on disk and what is running; it starts
    nothing and opens no program.
    """
    jobs = jobs or JOBS
    combo = run.state.get("combos", {}).get(combo_id, {})
    groups = []
    for group in GROUPS:
        job = jobs.get(run.run_id, combo_id, group)
        files = []
        for spec in (s for s in CATALOG if s.group == group):
            path = path_of(run, combo_id, spec.key)
            files.append({
                "key": spec.key, "label": spec.label, "description": spec.description,
                "exists": path is not None,
                "size": os.path.getsize(path) if path else None,
                "size_text": human_size(os.path.getsize(path)) if path else None,
                "mime": spec.mime,
            })
        missing = [f for f in files if not f["exists"]]
        can, why = can_generate(run, combo_id, group)
        groups.append({
            "group": group, "title": GROUP_TITLES[group], "files": files,
            "missing": len(missing),
            "can_generate": bool(can),
            "cannot_reason": None if can else why,
            "job": {k: job[k] for k in ("state", "message", "error")} if job else None,
        })
    return groups


def can_generate(run, combo_id: str, group: str):
    """(True, None) or (False, why). A combination whose geometry never
    got built has nothing to make files from."""
    combo = run.state.get("combos", {}).get(combo_id)
    if combo is None:
        return False, "unknown combination"
    if not os.path.isfile(os.path.join(run.combo_path(combo_id), "dimensions.txt")):
        return False, "this combination's geometry has not been built yet"
    if group in PART_OF_GROUP and not (combo.get("step_status") == "done"):
        return False, "the box and lid have not been built for this combination"
    return True, None


# -- values for a combination ------------------------------------------

def combo_values(run, combo_id: str) -> Dict[str, float]:
    """The numeric equations the combination was built from, read back from
    its own `dimensions.txt` snapshot."""
    path = os.path.join(run.combo_path(combo_id), "dimensions.txt")
    return eq.get_numeric_parameters(eq.read_file(path))


def combo_materials(run) -> Dict[str, object]:
    return dict((run.analysis_settings or {}).get("materials") or {})


# -- file contents -------------------------------------------------------

def project_text(pcb_name: str) -> str:
    """A minimal Altium PCB project (.PrjPcb) holding one board."""
    return "\r\n".join([
        "[Design]",
        "Version=1.0",
        "HierarchyMode=0",
        "ChannelRoomNamingStyle=0",
        "OutputPath=",
        "LogFolderPath=",
        "ReleasesFolder=",
        "ChannelDesignatorFormat=$Component_$RoomName",
        "ChannelRoomLevelSeperator=_",
        "OpenOutputs=1",
        "ArchiveProject=0",
        "TimestampOutput=0",
        "SeparateFolders=0",
        "TemplateLocationPath=",
        "PinSwapBy_Netlabel=1",
        "PinSwapBy_Pin=1",
        "",
        "[Document1]",
        f"DocumentPath={pcb_name}",
        "AnnotationEnabled=1",
        "AnnotateStartValue=1",
        "AnnotationIndexControlEnabled=0",
        "AnnotateSuffix=",
        "AnnotateScope=All",
        "AnnotateOrder=-1",
        "DoLibraryUpdate=1",
        "DoDatabaseUpdate=1",
        "ClassGenCCAutoEnabled=1",
        "ClassGenCCAutoRoomEnabled=1",
        "ClassGenNCAutoScope=None",
        "DItemRevisionGUID=",
        "GenerateClassCluster=0",
        "DocumentUniqueId=",
        "",
    ])


def write_zip(destination: str, members: Dict[str, object]) -> None:
    """Zip `members` ({archive name: file path or bytes/str content}) to
    `destination`, written atomically so a half-written zip never lists
    as a finished file."""
    temporary = destination + ".part"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, source in members.items():
            if isinstance(source, str) and os.path.isfile(source):
                archive.write(source, name)
            elif isinstance(source, str):
                archive.writestr(name, source)
            else:
                archive.writestr(name, source)
    os.replace(temporary, destination)


def add_read_me(members: Dict[str, object], run, combo_id: str, what: str) -> None:
    members["README.txt"] = (
        f"{what}\r\nRun {run.run_id}, combination {combo_id}\r\n"
        f"Generated {_dt.datetime.now().isoformat(timespec='seconds')} by microwave_package.\r\n"
        "Units: millimetres. Board centre at the origin.\r\n"
    )


# -- producers -----------------------------------------------------------

def _com_thread():
    """Context for COM on a worker thread (the producers drive SolidWorks)."""
    import contextlib

    @contextlib.contextmanager
    def guard():
        try:
            import pythoncom
        except ImportError:
            yield
            return
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
    return guard()


def make_group(run, combo_id: str, group: str, log: Callable[[str], None]) -> None:
    """Generate whatever is missing in `group`. Raises on failure."""
    if group not in GROUPS:
        raise ValueError(f"unknown group {group!r}")
    ok, why = can_generate(run, combo_id, group)
    if not ok:
        raise RuntimeError(why)
    with APP_LOCK:
        if group in PART_OF_GROUP:
            with _com_thread():
                make_cad_group(run, combo_id, group, log)
        else:
            make_pcb(run, combo_id, log)


def make_cad_group(run, combo_id: str, group: str, log: Callable[[str], None]) -> None:
    from ..design import solidworks_manufacturing

    part = PART_OF_GROUP[group]
    wanted = {"sldprt": target_path(run, combo_id, f"{group}_sldprt"),
              "drawing": target_path(run, combo_id, f"{group}_drawing")}
    missing = {k: v for k, v in wanted.items() if not os.path.isfile(v)}
    if not missing:
        log("already made")
        return
    solidworks_manufacturing.export_part(
        part=part,
        equations_path=os.path.join(run.combo_path(combo_id), "dimensions.txt"),
        outputs=missing,
        title=f"{run.run_id} / {combo_id}",
        values=combo_values(run, combo_id),
        log=log,
    )


def make_pcb(run, combo_id: str, log: Callable[[str], None]) -> None:
    from ..design import altium_generation, fab_drawing, gerber

    combo_path = run.combo_path(combo_id)
    values = combo_values(run, combo_id)
    materials = combo_materials(run)
    name = file_stem(run, combo_id)

    pcbdoc = os.path.join(combo_path, "package.PcbDoc")
    copper_path = os.path.join(combo_path, "copper.txt")
    if not os.path.isfile(pcbdoc) or not os.path.isfile(copper_path):
        log("redrawing the board in Altium (this combination kept no PcbDoc)")
        ok, reason = altium_generation.generate_for_combo(
            run.combo_dir(combo_id), values, materials=materials, log=log)
        if not ok:
            raise RuntimeError(reason)
        if not os.path.isfile(pcbdoc):
            raise RuntimeError("Altium drew the board but left no package.PcbDoc")

    with open(copper_path, "r", encoding="utf-8") as f:
        copper = fab_drawing.parse_package_copper(f.read())

    # -- the files that need no program: written from the copper data
    log("writing Gerber, drill and the fabrication drawing")
    fab = gerber.fab_set(values, copper, name)
    gerbers = {n: t for n, t in fab.items() if n.endswith(".gbr")}
    drills = {n: t for n, t in fab.items() if n.endswith(".drl")}
    add_read_me(gerbers, run, combo_id, "Gerber RS-274X, 4.6 metric, top/bottom copper and outline.")
    add_read_me(drills, run, combo_id, "Excellon drill: _PTH plated (vias), _NPTH non-plated (screw clearance).")
    write_zip(target_path(run, combo_id, "pcb_gerber"), gerbers)
    write_zip(target_path(run, combo_id, "pcb_drill"), drills)

    fab_drawing.build(values, str(materials.get("mat_pcb", "")), copper,
                      run.run_id, combo_id).save(target_path(run, combo_id, "pcb_fab_drawing"))

    pcb_name = name + "_pcb.PcbDoc"
    project = {pcb_name: pcbdoc, name + "_pcb.PrjPcb": project_text(pcb_name)}
    add_read_me(project, run, combo_id, "Altium Designer project: open the .PrjPcb.")
    write_zip(target_path(run, combo_id, "pcb_project"), project)

    # -- the formats only Altium writes
    log("exporting ODB++, IPC-2581 and STEP from Altium")
    altium_generation.export_manufacturing(
        pcbdoc, output_dir(run, combo_id), name, log=log,
    )
