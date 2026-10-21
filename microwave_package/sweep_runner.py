"""
Long unattended sweeps: run a plan of pipeline jobs one after another, check
every result, and watch the worker and the desktop in between.

    python -m microwave_package.sweep_runner sweeps/examples/depth_converged.json
    python -m microwave_package.sweep_runner PLAN.json --dry-run

A plan is a JSON file (examples in sweeps/examples/, the runbook in SWEEPS.md).
Everything the runner writes goes to sweeps/<plan name>/:

    state.json      one record per job; a restart resumes from it
    results.csv     one row per job and combination, rebuilt after every job
    results.md      the same, for reading, with the date and band edges
    alerts.txt      what needs a person: a locked screen, a dialog, a stale worker
    runner.log      everything the runner did
    status.json     rewritten every minute: current job, worker, queue, alerts
    logs/           the pipeline's own output, one file per attempt
    STOP, PAUSE     control files you create to stop or pause the sweep

Before each job the runner waits, alerting once per problem, until the worker
heartbeat is fresh, the queue is reachable, the screen is unlocked and no
Altium dialog is open. It then starts the pipeline as its own child process
and supervises it. A child that fails while the screen was locked, an Altium
dialog was open, or SolidWorks refused a document is resumed with --resume
(at most three times per job) instead of being recorded as failed.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import psutil

from . import paths, pipeline, solver_presets
from .design.sweep import expand_values
from .hfss.driven import s11_analysis

SWEEPS_ROOT = paths.PROJECT_ROOT / "sweeps"
STOP_FILE = "STOP"
PAUSE_FILE = "PAUSE"

DEFAULT_JOB_HOURS = 16.0
HEARTBEAT_PREFLIGHT_MIN = 15.0   # a job starts only with a heartbeat younger than this
HEARTBEAT_ALERT_MIN = 30.0       # during a job: alert past this
HANG_ALERT_MIN = 90.0            # heartbeat stale AND no worker log line for this long
PREFLIGHT_EVERY_S = 60.0
WATCH_EVERY_S = 60.0
TICK_S = 5.0
KILL_GRACE_S = 60.0
MAX_INTERRUPTIONS = 3            # resumes per job after a transient failure
FLAT_SPAN_DB = 1e-3              # S21 spread below this over every point is a flat result
CLEAN_LEVEL_DB = -3.0            # "clean" points: S21 above this

# The bands the reports use. The last one leaves out 20 GHz itself, as the
# earlier reports did (s11_analysis.BANDS).
BANDS = s11_analysis.BANDS[:3]
BAND_KEYS = ("2_10", "10_17", "17_20")
BAND_LABELS = ("2-10", "10-17", "17-20")

GEOMETRY_CAUSE = "the geometry was built but the queue was unreachable, so nothing was handed over"
ERROR_TEXT = "Error in Solving"
TASK_ID_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*?_\d{8}_\d{6}_\d{6})\b")
PROCESSING_RE = re.compile(r"Processing task ([A-Za-z][A-Za-z0-9_-]*?_\d{8}_\d{6}_\d{6})\b")
RUN_ID_RE = re.compile(r"(?:Created|Resuming) run (run_[0-9A-Za-z_]+)")
# Failures a resume cures: the pipeline printed one of these and stopped.
TRANSIENT = (
    ("script macro error", "AEDT copy/paste failed ('script macro error'): the screen was locked or a dialog was open"),
    ("would not take a script", "Altium would not take a script: a dialog was open in Altium"),
    ("OpenDoc6", "SolidWorks refused a document (OpenDoc6): documents from another worktree were open"),
)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PLAN_KEYS = {"name", "preset", "max_job_hours", "description", "jobs"}
JOB_KEYS = {"name", "args", "note"}
COMBO_COLUMNS = (
    "date", "plan", "job", "combo", "run_id", "status", "valid", "reason", "params",
    "preset", "overrides", "solver",
    "points_2_10", "points_10_17", "points_17_20",
    "s11_2_10_db", "s11_10_17_db", "s11_17_20_db",
    "s21_2_10_db", "s21_10_17_db", "s21_17_20_db",
    "clean_2_10_pct", "clean_10_17_pct", "clean_17_20_pct",
)
EXIT_OK, EXIT_FAILURES, EXIT_PLAN, EXIT_BUSY = 0, 1, 2, 3


# ---------------------------------------------------------------- plans ----

class PlanError(ValueError):
    """A plan that cannot run. Reported before anything is started."""


@dataclasses.dataclass(frozen=True)
class Job:
    name: str
    args: Tuple[str, ...]
    note: str = ""


@dataclasses.dataclass(frozen=True)
class Plan:
    name: str
    preset: str
    max_job_hours: float
    description: str
    jobs: Tuple[Job, ...]
    source: str = ""


def _check_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise PlanError(f"{what} {value!r} must be 1 to 64 letters, digits, '_', '.' or '-', "
                        "starting with a letter or digit")
    return value


def job_settings(args: Sequence[str], preset: str) -> dict:
    """The solver settings a job's arguments give, read by the pipeline's own
    parser, so a flag the pipeline does not know is refused here, not in the
    middle of a weekend. Raises PlanError."""
    args = list(args)
    for item in args:
        if not isinstance(item, str):
            raise PlanError("every argument must be a string")
        flag = item.split("=", 1)[0]
        if flag in ("--preset", "--resume"):
            raise PlanError(f"{flag} is set by the plan (its preset) or by the runner (resume); "
                            "remove it from the job's arguments")
    if not any(item == "--sweep" or item.startswith("--sweep=") for item in args):
        raise PlanError("a job needs at least one --sweep (a single value pins that parameter)")
    for index, item in enumerate(args):
        if item == "--sweep" and index + 1 < len(args):
            _check_sweep(args[index + 1])
        elif item.startswith("--sweep="):
            _check_sweep(item.split("=", 1)[1])
    err = StringIO()
    try:
        with redirect_stderr(err):
            namespace = pipeline.parse_args(["--preset", preset, *args])
            settings = pipeline.build_analysis_settings(namespace)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            message = exc.code
        else:
            lines = [line for line in err.getvalue().strip().splitlines() if line.strip()]
            message = lines[-1] if lines else "the pipeline refused these arguments"
        raise PlanError(f"the pipeline refuses these arguments: {message}") from None
    return settings


def _check_sweep(spec: str) -> None:
    if "=" not in spec:
        raise PlanError(f"--sweep {spec!r} must be NAME=SPEC, e.g. pedestal_depth=0.1,0.25")
    name, _, values = spec.partition("=")
    try:
        expand_values(values)
    except ValueError as exc:
        raise PlanError(f"--sweep {name}: {exc}") from None


def parse_plan(data: Any, source: str = "") -> Plan:
    if not isinstance(data, dict):
        raise PlanError("the plan must be a JSON object")
    unknown = sorted(set(data) - PLAN_KEYS)
    if unknown:
        raise PlanError(f"unknown plan key(s): {', '.join(unknown)}")
    name = _check_name(data.get("name"), "plan name")
    preset = data.get("preset")
    if preset not in solver_presets.PRESET_NAMES:
        raise PlanError(f"preset must be one of {', '.join(solver_presets.PRESET_NAMES)}, not {preset!r}")
    hours = data.get("max_job_hours", DEFAULT_JOB_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
        raise PlanError("max_job_hours must be a positive number of hours")
    jobs_data = data.get("jobs")
    if not isinstance(jobs_data, list) or not jobs_data:
        raise PlanError("jobs must be a non-empty list")
    jobs: List[Job] = []
    seen = set()
    for index, item in enumerate(jobs_data, 1):
        if not isinstance(item, dict):
            raise PlanError(f"job {index} must be an object with a name and args")
        extra = sorted(set(item) - JOB_KEYS)
        if extra:
            raise PlanError(f"job {index}: unknown key(s) {', '.join(extra)}")
        job_name = _check_name(item.get("name"), f"job {index} name")
        if job_name in seen:
            raise PlanError(f"job name {job_name!r} is used twice")
        seen.add(job_name)
        args = item.get("args")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise PlanError(f"job {job_name}: args must be a list of strings")
        try:
            job_settings(args, preset)
        except PlanError as exc:
            raise PlanError(f"job {job_name}: {exc}") from None
        jobs.append(Job(job_name, tuple(args), str(item.get("note", ""))))
    return Plan(name=name, preset=preset, max_job_hours=float(hours),
                description=str(data.get("description", "")), jobs=tuple(jobs), source=source)


def load_plan(path: Path) -> Plan:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"cannot read the plan {path}: {exc}") from None
    except ValueError as exc:
        raise PlanError(f"the plan {path} is not valid JSON: {exc}") from None
    return parse_plan(data, source=str(path))


def job_command(plan: Plan, job: Job, run_id: Optional[str], python: str = sys.executable) -> List[str]:
    """The pipeline command for one job: a fresh run, or a resume of the run it made."""
    base = [python, "-u", "-m", "microwave_package.pipeline"]
    if run_id:
        return base + ["--resume", run_id]
    return base + ["--preset", plan.preset, *job.args]


# ---------------------------------------------------------- small helpers ----

def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def read_json(path: Path, attempts: int = 10) -> Optional[dict]:
    """A JSON file, or None if it is absent. Retries while another process is replacing it."""
    for attempt in range(attempts):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05 * (attempt + 1))
    return None


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    # Something holds the file open (a reader). Losing one status write is
    # better than stopping the sweep, so write in place as a last resort.
    path.write_text(temporary.read_text(encoding="utf-8"), encoding="utf-8")
    temporary.unlink(missing_ok=True)


def read_text_tail(path: Optional[Path], max_bytes: int = 400_000) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def last_error_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if "ERROR" in line or "Error" in line or "Traceback" in line:
            return line[-240:]
    return lines[-1][-240:] if lines else ""


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def main_checkout_of(root: Path) -> Path:
    """The main checkout that a worktree folder (.claude/worktrees/NAME) belongs to."""
    parts = Path(root).parts
    for index in range(len(parts) - 1):
        if parts[index].lower() == ".claude" and parts[index + 1].lower() == "worktrees":
            return Path(*parts[:index])
    return Path(root)


def owning_checkout(document: str, main_root: Path) -> Optional[str]:
    """The worktree folder (or the main checkout) a SolidWorks document lives in,
    or None when it is outside this repository."""
    if not document:
        return None
    doc = _norm(document)
    trees = _norm(Path(main_root) / ".claude" / "worktrees") + os.sep
    if doc.startswith(trees):
        return trees + doc[len(trees):].split(os.sep)[0]
    main = _norm(main_root) + os.sep
    if doc.startswith(main):
        return _norm(main_root)
    return None


def foreign_documents(documents: Sequence[Tuple[str, bool]], project_root: Path,
                      main_root: Path) -> List[str]:
    """The open documents that belong to another checkout than this runner's."""
    own = _norm(project_root)
    found = []
    for path, _dirty in documents:
        owner = owning_checkout(path, main_root)
        if owner is not None and owner != own:
            found.append(path)
    return found


# ------------------------------------------------------------ the worker ----

class Heartbeat:
    """Age of the worker's heartbeat file, measured on this PC's clock.

    The file is rewritten every few seconds while the worker lives. Its
    timestamp comes from the file server, so judging its age against this
    PC's clock would be wrong by whatever the two clocks disagree by. Instead
    the age is the time since this runner last saw the timestamp change. Until
    it has seen one change (the first look after a start), the timestamp is
    used, clamped at zero.
    """

    def __init__(self) -> None:
        self.mtime: Optional[float] = None
        self.changed_at: Optional[float] = None

    def observe(self, mtime: Optional[float], now: float) -> None:
        if mtime is None:
            self.mtime = None
            self.changed_at = None
            return
        if self.mtime is not None and mtime != self.mtime:
            self.changed_at = now
        self.mtime = mtime

    def age_minutes(self, now: float) -> Optional[float]:
        if self.mtime is None:
            return None
        if self.changed_at is not None:
            seconds = now - self.changed_at
        else:
            seconds = now - self.mtime
        return max(0.0, seconds) / 60.0


class LogGrowth:
    """Minutes since the worker log last grew, on this PC's clock."""

    def __init__(self) -> None:
        self.size: Optional[int] = None
        self.changed_at: Optional[float] = None

    def start(self, size: Optional[int], now: float) -> None:
        self.size = size
        self.changed_at = now

    def update(self, size: Optional[int], now: float) -> Optional[float]:
        if size is None:
            return None
        if self.size is None or size != self.size:
            self.size = size
            self.changed_at = now
        if self.changed_at is None:
            return 0.0
        return max(0.0, now - self.changed_at) / 60.0


def count_errors_by_task(text: str) -> Dict[str, int]:
    """How many 'Error in Solving' lines each task logged in its LAST processing block.

    The worker logs no task id on the error line, so a line belongs to the task
    whose 'Processing task' block it falls in. A task can be processed again
    (a recovered orphan, a retry); only its last block counts, because that is
    the solve whose result is kept.
    """
    counts: Dict[str, int] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        processing = PROCESSING_RE.search(line)
        if processing:
            current = processing.group(1)
            counts[current] = 0
            continue
        mention = TASK_ID_RE.search(line)
        if mention:
            current = mention.group(1)
            counts.setdefault(current, 0)
        if ERROR_TEXT in line and current is not None:
            counts[current] += 1
    return counts


def worker_errors_since(log_path: Path, offset: int) -> Optional[Dict[str, int]]:
    """count_errors_by_task over the worker log from a byte offset on, or None if unreadable."""
    try:
        size = os.path.getsize(log_path)
        start = offset if size >= offset else 0
        with open(log_path, "rb") as handle:
            handle.seek(start)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return count_errors_by_task(text)


class Desktop:
    """What the runner needs to know about the worker and this desktop.

    Read-only, except close_solidworks_documents, which the runner calls only
    for documents from another checkout, without saving them.
    """

    def __init__(self, queue_root: Path, clock: Callable[[], float] = time.time) -> None:
        self.queue_root = Path(queue_root)
        self.logs = self.queue_root / "_logs"
        self.heartbeat_path = self.logs / "worker_status.json"
        self.worker_log_path = self.logs / "ansys_analyze_worker.log"
        self.clock = clock
        self.heartbeat = Heartbeat()
        self.notes: List[str] = []

    def drain_notes(self) -> List[str]:
        """What the desktop probes could not read since the last call; the runner journals them."""
        notes, self.notes = self.notes, []
        return notes

    def altium_running(self) -> bool:
        return any((proc.info.get("name") or "").upper() == "X2.EXE"
                   for proc in psutil.process_iter(["name"]))

    def queue_reachable(self) -> bool:
        return os.path.isdir(self.queue_root)

    def heartbeat_age_minutes(self) -> Optional[float]:
        try:
            mtime: Optional[float] = os.stat(self.heartbeat_path).st_mtime
        except OSError:
            mtime = None
        now = self.clock()
        self.heartbeat.observe(mtime, now)
        return self.heartbeat.age_minutes(now)

    def heartbeat_detail(self) -> Dict[str, Any]:
        data = read_json(self.heartbeat_path, attempts=3) or {}
        return {"worker_state": data.get("state"), "current_task_id": data.get("current_task_id")}

    def worker_log_size(self) -> Optional[int]:
        try:
            return os.path.getsize(self.worker_log_path)
        except OSError:
            return None

    def worker_errors(self, offset: int) -> Optional[Dict[str, int]]:
        return worker_errors_since(self.worker_log_path, offset)

    def queue_counts(self) -> Optional[Dict[str, Optional[int]]]:
        if not self.queue_reachable():
            return None

        def count(sub: str) -> Optional[int]:
            try:
                return sum(1 for name in os.listdir(self.queue_root / sub) if name.endswith(".task.json"))
            except OSError:
                return None

        return {"pending": count("pending"), "in_progress": count("in_progress")}

    def screen_locked(self) -> bool:
        return any((proc.info.get("name") or "").lower() == "logonui.exe"
                   for proc in psutil.process_iter(["name"]))

    def altium_modal_titles(self) -> List[str]:
        """Titles of the visible message dialogs (TMessageForm) that Altium has open."""
        altium = {proc.info["pid"] for proc in psutil.process_iter(["name", "pid"])
                  if (proc.info.get("name") or "").upper() == "X2.EXE"}
        if not altium:
            return []
        return [title or "(untitled)" for pid, cls, title in _visible_windows()
                if pid in altium and cls == "TMessageForm"]

    def _sw_running(self) -> bool:
        return any((proc.info.get("name") or "").upper() == "SLDWORKS.EXE"
                   for proc in psutil.process_iter(["name"]))

    def solidworks_documents(self) -> List[Tuple[str, bool]]:
        """(path, unsaved changes) of every document SolidWorks has open. Empty when it is not running.

        GetActiveObject, not Dispatch: Dispatch would start SolidWorks.
        """
        if not any((proc.info.get("name") or "").upper() == "SLDWORKS.EXE"
                   for proc in psutil.process_iter(["name"])):
            return []
        app = _solidworks_app(running=True)
        if app is None:
            self.notes.append("SolidWorks is running but its COM object could not be attached: no document "
                              "check was made for this job")
            return []
        documents: List[Tuple[str, bool]] = []
        try:
            doc = app.GetFirstDocument()
            while doc is not None and len(documents) < 500:
                try:
                    path = str(doc.GetPathName() or "")
                except Exception as exc:
                    self.notes.append(f"SolidWorks: a document's path could not be read ({exc}); "
                                      "it cannot be judged foreign")
                    path = ""
                try:
                    dirty = bool(doc.GetSaveFlag())
                except Exception:
                    dirty = False
                documents.append((path, dirty))
                doc = doc.GetNext()
        except Exception as exc:
            self.notes.append(f"SolidWorks: the document list stopped early ({exc})")
        return documents

    def close_solidworks_documents(self, paths_to_close: Sequence[str]) -> List[Tuple[str, str]]:
        """Close these documents WITHOUT saving. Returns (path, error) for each that failed."""
        app = _solidworks_app(running=self._sw_running())
        failures: List[Tuple[str, str]] = []
        if app is None:
            return [(path, "SolidWorks is no longer running") for path in paths_to_close]
        for path in paths_to_close:
            try:
                app.CloseDoc(os.path.basename(path))   # the title: the file name, as the design code uses
            except Exception as exc:
                failures.append((path, str(exc)))
        return failures

    def create_time(self, pid: int) -> Optional[float]:
        try:
            return psutil.Process(pid).create_time()
        except psutil.Error:
            return None

    def process_alive(self, pid: Optional[int], create_time: Optional[float]) -> bool:
        if not pid:
            return False
        try:
            proc = psutil.Process(pid)
            return create_time is None or abs(proc.create_time() - create_time) < 1.0
        except psutil.Error:
            return False


def _visible_windows() -> List[Tuple[int, str, str]]:
    """(pid, class name, title) of every visible top-level window. Windows only."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    handles: List[int] = []

    def collect(hwnd, _lparam):
        handles.append(hwnd)
        return True

    user32.EnumWindows(callback_type(collect), 0)
    found = []
    for hwnd in handles:
        if not user32.IsWindowVisible(hwnd):
            continue
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        found.append((pid.value, cls.value, title.value))
    return found


_COM_READY = False


def _solidworks_app(running: bool):
    """The SolidWorks session to read or close documents in, or None. Only when SolidWorks is running.

    GetActiveObject first. On this PC GetActiveObject cannot see the running session, so Dispatch is
    the fallback: it attaches to the running session, as the pipeline's own SolidWorks step does.
    Nothing is started: without a running SolidWorks this returns None.
    """
    if not running:
        return None
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    global _COM_READY
    if not _COM_READY:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
        _COM_READY = True
    try:
        return win32com.client.GetActiveObject("SldWorks.Application")
    except Exception:
        pass
    try:
        return win32com.client.Dispatch("SldWorks.Application")
    except Exception:
        return None


# ------------------------------------------------------------ the results ----

def describe_params(params: Dict[str, Any]) -> str:
    return "; ".join(f"{key}={value:g}" if isinstance(value, (int, float)) else f"{key}={value}"
                     for key, value in params.items())


def solver_text(settings: Dict[str, Any]) -> str:
    return " ".join(f"{key}={settings.get(key)}" for key in solver_presets.SOLVER_KEYS)


def band_stats(freq: np.ndarray, s11: np.ndarray, s21: np.ndarray) -> Dict[str, List[Any]]:
    """Per band: points, mean S11 dB, mean S21 dB, and the clean share (% of points with S21 above -3 dB)."""
    s11_db = 20.0 * np.log10(np.abs(s11))
    s21_db = 20.0 * np.log10(np.abs(s21))
    stats: Dict[str, List[Any]] = {"points": [], "s11": [], "s21": [], "clean": []}
    for band in BANDS:
        mask = s11_analysis.in_band(freq, band)
        count = int(mask.sum())
        stats["points"].append(count)
        if count:
            stats["s11"].append(float(np.mean(s11_db[mask])))
            stats["s21"].append(float(np.mean(s21_db[mask])))
            stats["clean"].append(100.0 * float(np.mean(s21_db[mask] > CLEAN_LEVEL_DB)))
        else:
            stats["s11"].append(None)
            stats["s21"].append(None)
            stats["clean"].append(None)
    return stats


def read_sweep(folder: Path, combo_id: str) -> Tuple[Optional[Any], Optional[str]]:
    """The coarse sweep of one combination, or (None, why not)."""
    path = Path(folder) / "models" / combo_id / "results.csv"
    if not path.exists():
        return None, "no results.csv: the solve did not come back (or was not copied and read)"
    try:
        sweep = s11_analysis.load_sweep(Path(folder), combo_id, coarse_only=True)
    except (OSError, KeyError, ValueError) as exc:
        return None, f"results unreadable: {exc}"
    if sweep.freq_ghz.size == 0:
        return None, "results.csv has no coarse frequency points"
    return sweep, None


def judge_combo(folder: Path, combo_id: str, combo: dict, eigen_combo: Optional[dict],
                errors: Optional[Dict[str, int]], finished: bool) -> Tuple[str, str, Optional[dict], Optional[float]]:
    """(status, reason, band stats, S21 span) for one combination.

    status: skipped (the geometry does not fit), valid, invalid (the solve is
    not to be trusted), failed (it did not produce a result), or pending (it
    has not finished -- only possible when the job did not finish).
    """
    if combo.get("skip_reason"):
        return "skipped", str(combo["skip_reason"]), None, None
    unfinished = "failed" if finished else "pending"
    task = combo.get("task_id")
    eigen_task = (eigen_combo or {}).get("task_id")
    if not task:
        return unfinished, (f"not queued (model {combo.get('model_status') or 'not built'}, "
                            f"geometry {combo.get('step_status') or 'not built'})"), None, None
    if combo.get("queue_status") == "failed":
        return "failed", "the worker's task failed", None, None
    if eigen_combo and eigen_combo.get("queue_status") == "failed":
        return "failed", "the paired eigenmode task failed, and the driven run uses its modes", None, None
    if errors is None:
        return "failed", "the worker log could not be read, so the solve cannot be confirmed", None, None
    unseen = [name for name in (task, eigen_task) if name and name not in errors]
    if unseen:
        return unfinished, (f"worker log has no record of task {unseen[0]}, so the solve cannot be "
                            "confirmed"), None, None
    sweep, problem = read_sweep(folder, combo_id)
    if problem is not None:
        return unfinished, problem, None, None
    s21_db = 20.0 * np.log10(np.abs(sweep.s21))
    span = float(np.max(s21_db) - np.min(s21_db))
    stats = band_stats(sweep.freq_ghz, sweep.s11, sweep.s21)
    own_errors = errors.get(task, 0)
    eigen_errors = errors.get(eigen_task, 0) if eigen_task else 0
    if own_errors or eigen_errors:
        where = "its task" if own_errors else "the paired eigenmode task"
        count = own_errors or eigen_errors
        return ("invalid", f"the worker logged '{ERROR_TEXT}' for {where} ({count} time(s)), "
                           "although it marked the task done", stats, span)
    if span < FLAT_SPAN_DB:
        return ("invalid", f"S21 is flat: identical at all {sweep.freq_ghz.size} points "
                           f"(spread {span:.1e} dB)", stats, span)
    return "valid", "", stats, span


def evaluate_run(runs_root: Path, run_id: str, errors: Optional[Dict[str, int]], finished: bool,
                 plan_name: str, job_name: str, day: str) -> List[dict]:
    """One row per combination of a run (and what its paired eigenmode run did)."""
    folder = Path(runs_root) / run_id
    driven_state = read_json(folder / "state.json")
    if not driven_state:
        return []
    driven_config = read_json(folder / "run_config.json") or {}
    settings = driven_config.get("analysis_settings", {})
    eigen_id = driven_config.get("eigen_run_id")
    eigen_state = read_json(Path(runs_root) / eigen_id / "state.json") if eigen_id else None
    eigen_combos = (eigen_state or {}).get("combos", {})
    rows = []
    for combo_id, combo in driven_state.get("combos", {}).items():
        status, reason, stats, span = judge_combo(
            folder, combo_id, combo, eigen_combos.get(combo_id), errors, finished)
        row: Dict[str, Any] = {
            "date": day, "plan": plan_name, "job": job_name, "combo": combo_id, "run_id": run_id,
            "status": status,
            "valid": "yes" if status == "valid" else ("no" if status in ("invalid", "failed") else ""),
            "reason": reason, "params": describe_params(combo.get("params") or {}),
            "preset": settings.get("solver_preset") or "none",
            "overrides": ";".join(settings.get("solver_overrides") or []),
            "solver": solver_text(settings),
        }
        for index, key in enumerate(BAND_KEYS):
            row[f"points_{key}"] = stats["points"][index] if stats else None
            row[f"s11_{key}_db"] = _round(stats["s11"][index], 3) if stats else None
            row[f"s21_{key}_db"] = _round(stats["s21"][index], 3) if stats else None
            row[f"clean_{key}_pct"] = _round(stats["clean"][index], 1) if stats else None
        rows.append(row)
    return rows


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def job_verdict(rows: List[dict]) -> Tuple[str, str]:
    """The job's status from its combinations: done, invalid or failed."""
    if not rows:
        return "failed", "the run has no combinations"
    live = [row for row in rows if row["status"] != "skipped"]
    if not live:
        return "failed", "every combination was skipped (its geometry does not fit)"
    failed = [row for row in live if row["status"] in ("failed", "pending")]
    if failed:
        first = failed[0]
        return "failed", (f"{len(failed)} of {len(live)} combination(s) did not produce a result; "
                          f"first {first['combo']}: {first['reason']}")
    invalid = [row for row in live if row["status"] == "invalid"]
    if invalid:
        first = invalid[0]
        return "invalid", (f"{len(invalid)} of {len(live)} combination(s) are not valid; "
                           f"first {first['combo']}: {first['reason']}")
    return "done", ""


def render_results(folder: Path, plan: Plan, rows: List[dict], now: float) -> None:
    """results.csv and results.md, rebuilt from every combination of every job."""
    folder.mkdir(parents=True, exist_ok=True)
    csv_path = folder / "results.csv"
    temporary = csv_path.with_name("results.csv.tmp")
    with open(temporary, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMBO_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(temporary, csv_path)

    lines = [
        f"# Sweep results: {plan.name}",
        "",
        f"Written {stamp(now)} (local time) from the sweep state. Preset: {plan.preset}. "
        "One row per job and combination.",
        "",
        "Bands: 2-10 GHz [2,10]; 10-17 GHz [10,17]; 17-20 GHz [17,20) (20 GHz itself is not in the band, "
        "as in the earlier reports). Means and clean shares use the coarse sweep rows only. "
        "Clean = share of the band's points with S21 above -3 dB.",
        "",
        "| date | job | combo | status | S11 2-10 | S11 10-17 | S11 17-20 | S21 2-10 | S21 10-17 | "
        "S21 17-20 | clean 2-10 % | clean 10-17 % | clean 17-20 % | points 2-10 / 10-17 / 17-20 | reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def cell(value, digits=2):
        return "-" if value is None else f"{value:.{digits}f}"

    for row in rows:
        points = " / ".join("-" if row[f"points_{key}"] is None else str(row[f"points_{key}"])
                             for key in BAND_KEYS)
        lines.append(
            f"| {row['date']} | {row['job']} | {row['combo']} | {row['status']} | "
            + " | ".join(cell(row[f"s11_{key}_db"]) for key in BAND_KEYS) + " | "
            + " | ".join(cell(row[f"s21_{key}_db"]) for key in BAND_KEYS) + " | "
            + " | ".join(cell(row[f"clean_{key}_pct"], 1) for key in BAND_KEYS) + " | "
            + f"{points} | {row['reason'] or ''} |"
        )
    md_temporary = folder / "results.md.tmp"
    md_temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(md_temporary, folder / "results.md")


# ------------------------------------------------------------- the runner ----

class Episodes:
    """Alerts once per episode: a problem that persists raises one alert, and its clearing is noted once."""

    def __init__(self) -> None:
        self.active: set = set()

    def update(self, key: str, present: bool) -> Optional[str]:
        if present and key not in self.active:
            self.active.add(key)
            return "raise"
        if not present and key in self.active:
            self.active.discard(key)
            return "clear"
        return None


@dataclasses.dataclass
class Outcome:
    rc: Optional[int]
    timed_out: bool
    blocked: bool


class SweepRunner:
    def __init__(self, plan: Plan, folder: Path, *, desktop, project_root: Path, runs_root: Path,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 popen: Callable[..., Any] = subprocess.Popen, stop_on_error: bool = False,
                 echo: bool = True, dry_run: bool = False) -> None:
        self.plan = plan
        self.folder = Path(folder)
        self.desktop = desktop
        self.project_root = Path(project_root)
        self.main_root = main_checkout_of(self.project_root)
        self.runs_root = Path(runs_root)
        self.clock = clock
        self.sleep = sleep
        self.popen = popen
        self.stop_on_error = stop_on_error
        self.dry_run = dry_run
        self.journal = _Journal(self.folder, clock, echo)
        self.episodes = Episodes()
        self.state: Dict[str, Any] = {}
        self.state_name = "starting"
        self.current_job: Optional[str] = None
        self.last_alert: Optional[dict] = None
        self.obs: Dict[str, Any] = {}
        self.blocked = False
        self.log_growth = LogGrowth()
        self.stop_seen = False
        self.last_status_at: Optional[float] = None

    # -- state --------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.folder / "state.json"

    def load_state(self) -> None:
        existing = read_json(self.state_path)
        if existing is None:
            self.state = {"plan": self.plan.name, "preset": self.plan.preset,
                          "created": iso(self.clock()), "order": [], "jobs": {}}
        else:
            if existing.get("plan") != self.plan.name:
                raise PlanError(f"{self.state_path} belongs to plan {existing.get('plan')!r}")
            self.state = existing
        jobs = self.state.setdefault("jobs", {})
        for job in self.plan.jobs:
            record = jobs.get(job.name)
            if record is None:
                jobs[job.name] = new_job_record(job)
            elif record.get("args") != list(job.args):
                if record.get("status") in ("done", "invalid"):
                    self.journal.info(f"Job {job.name}: its arguments changed after it was {record['status']}; "
                                      "the finished result is kept.")
                else:
                    self.journal.alert(f"Job {job.name}: its arguments changed since it was last run; the "
                                       "earlier attempt is dropped (its run folder is kept on disk).")
                    jobs[job.name] = new_job_record(job)
        self.state["order"] = [job.name for job in self.plan.jobs]
        self.save()

    def save(self) -> None:
        self.state["updated"] = iso(self.clock())
        write_json_atomic(self.state_path, self.state)

    # -- status ---------------------------------------------------------------
    def write_status(self, state_name: Optional[str] = None, waiting: Optional[List[str]] = None,
                     force: bool = False) -> None:
        """status.json. Rewritten once a minute, and at once when forced (a job changed state)."""
        now = self.clock()
        if state_name:
            self.state_name = state_name
        if not force and self.last_status_at is not None and now - self.last_status_at < 60.0:
            return
        self.last_status_at = now
        self.obs["queue"] = self.desktop.queue_counts()
        jobs = self.state.get("jobs", {})
        counts = {key: 0 for key in ("done", "invalid", "failed", "timeout", "running", "pending")}
        for record in jobs.values():
            counts[record.get("status", "pending")] = counts.get(record.get("status", "pending"), 0) + 1
        record = jobs.get(self.current_job) if self.current_job else None
        started = record.get("started_epoch") if record else None
        payload = {
            "plan": self.plan.name,
            "preset": self.plan.preset,
            "updated": iso(now),
            "state": self.state_name,
            "waiting_for": waiting or [],
            "current_job": self.current_job,
            "current_status": record.get("status") if record else None,
            "job_started": iso(started) if started else None,
            "job_elapsed_min": round((now - started) / 60.0, 1) if started else None,
            "attempt": record.get("attempts") if record else None,
            "child_pid": record.get("child_pid") if record else None,
            "run_id": record.get("run_id") if record else None,
            "worker": {
                "heartbeat_age_min": _round(self.obs.get("heartbeat_age_min"), 1),
                "log_silent_min": _round(self.obs.get("log_silent_min"), 1),
                **self.obs.get("worker", {}),
            },
            "queue": self.obs.get("queue"),
            "jobs": {"total": len(self.plan.jobs), **counts},
            "last_alert": self.last_alert,
            "stop_file": (self.folder / STOP_FILE).exists(),
            "pause_file": (self.folder / PAUSE_FILE).exists(),
        }
        write_json_atomic(self.folder / "status.json", payload)

    def stop_requested(self) -> bool:
        return (self.folder / STOP_FILE).exists()

    def pause_requested(self) -> bool:
        return (self.folder / PAUSE_FILE).exists()

    def _episode(self, key: str, present: bool, text: str) -> None:
        event = self.episodes.update(key, present)
        if event == "raise":
            self.journal.alert(text)
            self.last_alert = {"time": iso(self.clock()), "text": text}
        elif event == "clear":
            self.journal.info(f"Resolved: {key}")

    # -- preflight ------------------------------------------------------------
    def check_problems(self, job_args: Optional[List[str]] = None) -> Dict[str, str]:
        """What stops a job starting now, keyed by problem. Read-only."""
        problems: Dict[str, str] = {}
        reachable = self.desktop.queue_reachable()
        age = self.desktop.heartbeat_age_minutes()
        self.obs["heartbeat_age_min"] = age
        self.obs["worker"] = self.desktop.heartbeat_detail()
        if not reachable:
            problems["queue"] = (f"the queue folder {self.desktop.queue_root} is not reachable from this PC; "
                                 "the job cannot be handed to the worker")
        if age is None:
            problems["heartbeat"] = "the worker heartbeat file is missing: the worker is not running or the queue share is down"
        elif age > HEARTBEAT_PREFLIGHT_MIN:
            problems["heartbeat"] = (f"the worker heartbeat is {age:.0f} min old (it must be under "
                                     f"{HEARTBEAT_PREFLIGHT_MIN:.0f} min): the worker PC may be off, asleep or hung")
        if self.desktop.screen_locked():
            problems["locked"] = ("Windows is locked (LogonUI.exe is running): unlock the PC. AEDT copy and "
                                  "paste fails on a locked screen")
        titles = self.desktop.altium_modal_titles()
        if titles:
            problems["modal"] = ("Altium has a dialog open (" + "; ".join(titles) + "): dismiss it. Altium "
                                 "refuses scripts while a dialog is open")
        if "--no-altium" not in (job_args or []) and not self.desktop.altium_running():
            problems["altium"] = ("Altium (X2.EXE) is not running: open it yourself. The runner never starts "
                                  "Altium, and a job's Altium step would start it")
        return problems

    def preflight(self, job_name: str) -> bool:
        """Wait until a job may start. False if STOP is asked for meanwhile."""
        last_check: Optional[float] = None
        paused_noted = False
        while True:
            if self.stop_requested():
                self.journal.info(f"STOP file found while waiting to start {job_name}: exiting.")
                return False
            if self.pause_requested():
                if not paused_noted:
                    self.journal.info(f"PAUSE file found: {job_name} will not start until the file is removed.")
                    paused_noted = True
                self.write_status("paused", waiting=["PAUSE file"])
                self.sleep(TICK_S)
                continue
            paused_noted = False
            now = self.clock()
            if last_check is None or now - last_check >= PREFLIGHT_EVERY_S:
                last_check = now
                problems = self.check_problems(self._job_args(job_name))
                for key in ("queue", "heartbeat", "locked", "modal", "altium"):
                    self._episode(f"preflight {key}", key in problems, problems.get(key, ""))
                if not problems:
                    self.journal.info(f"Preflight passed for {job_name}.")
                    return True
                self.write_status("waiting", waiting=sorted(problems.values()))
            self.sleep(TICK_S)

    def _job_args(self, job_name: str) -> Optional[List[str]]:
        return next((list(job.args) for job in self.plan.jobs if job.name == job_name), None)

    def wait_for_previous_child(self, job_name: str, record: dict) -> bool:
        """A pipeline process left by an earlier runner may still be working on this job.

        Never touched: the job waits for it to finish, then resumes.
        """
        pid, created = record.get("child_pid"), record.get("child_create_time")
        if not self.desktop.process_alive(pid, created):
            return True
        self._episode("previous child", True,
                      f"Job {job_name}: a pipeline process from an earlier runner (pid {pid}) is still "
                      "running. Waiting for it to finish; it is not touched.")
        while self.desktop.process_alive(pid, created):
            if self.stop_requested():
                return False
            self.write_status("waiting", waiting=[f"pipeline pid {pid} from an earlier runner"])
            self.sleep(TICK_S)
        self._episode("previous child", False, "")
        return True

    def close_foreign_solidworks(self, job_name: str) -> None:
        documents = self.desktop.solidworks_documents()
        for note in self.desktop.drain_notes():
            self.journal.alert(f"Before {job_name}: {note}")
        foreign = foreign_documents(documents, self.project_root, self.main_root)
        if not foreign:
            return
        self.journal.info(f"Before {job_name}: closing {len(foreign)} SolidWorks document(s) from another "
                          f"checkout WITHOUT saving: {', '.join(foreign)}")
        for path, error in self.desktop.close_solidworks_documents(foreign):
            self.journal.alert(f"could not close the SolidWorks document {path} ({error}). Close it by hand "
                               "without saving, or the next build can fail with OpenDoc6 error 65536.")

    # -- jobs -----------------------------------------------------------------
    def launch(self, cmd: List[str], log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
        with open(log_path, "wb") as handle:
            return self.popen(cmd, cwd=str(self.project_root), stdin=subprocess.DEVNULL,
                              stdout=handle, stderr=subprocess.STDOUT, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def supervise(self, proc, job_name: str, record: dict, log_path: Path) -> Outcome:
        limit = self.plan.max_job_hours * 3600.0
        started = self.clock()
        last_watch: Optional[float] = None
        self.blocked = False
        self.log_growth.start(self.desktop.worker_log_size(), started)
        while True:
            rc = proc.poll()
            if rc is not None:
                return Outcome(rc, False, self.blocked)
            now = self.clock()
            if now - started >= limit:
                self.journal.alert(f"Job {job_name}: the wall-time limit of {self.plan.max_job_hours:g} h is "
                                   "reached. The pipeline process started for this job is terminated (only that "
                                   "process); the job resumes on the next start.")
                proc.terminate()
                deadline = self.clock() + KILL_GRACE_S
                while proc.poll() is None and self.clock() < deadline:
                    self.sleep(1.0)
                if proc.poll() is None:
                    proc.kill()
                    deadline = self.clock() + KILL_GRACE_S
                    while proc.poll() is None and self.clock() < deadline:
                        self.sleep(1.0)
                return Outcome(proc.poll(), True, self.blocked)
            if last_watch is None or now - last_watch >= WATCH_EVERY_S:
                last_watch = now
                self.watch(job_name, record, log_path)
                self.write_status("running")
            self.sleep(TICK_S)

    def watch(self, job_name: str, record: dict, log_path: Path) -> None:
        """One minute of the watchdog: the child's run id, the worker, the desktop."""
        now = self.clock()
        if not record.get("run_id"):
            found = RUN_ID_RE.search(read_text_tail(log_path))
            if found:
                record["run_id"] = found.group(1)
                self.journal.info(f"Job {job_name}: run {found.group(1)}")
                self.save()
        age = self.desktop.heartbeat_age_minutes()
        silent = self.log_growth.update(self.desktop.worker_log_size(), now)
        locked = self.desktop.screen_locked()
        titles = self.desktop.altium_modal_titles()
        if locked or titles:
            self.blocked = True
        self.obs["heartbeat_age_min"] = age
        self.obs["log_silent_min"] = silent
        self.obs["worker"] = self.desktop.heartbeat_detail()
        stale = age is None or age > HEARTBEAT_ALERT_MIN
        age_text = "missing" if age is None else f"{age:.0f} min old"
        self._episode("watch heartbeat", stale,
                      f"Job {job_name}: the worker heartbeat is {age_text} (over {HEARTBEAT_ALERT_MIN:.0f} min). "
                      "The worker may be hung or its PC asleep; the job keeps waiting.")
        hung = (age is None or age > HANG_ALERT_MIN) and silent is not None and silent > HANG_ALERT_MIN
        self._episode("watch hung", hung,
                      f"Job {job_name}: the worker looks hung: no new worker log line for {silent or 0:.0f} min "
                      f"and the heartbeat is {age_text}. Check the worker PC. The runner keeps waiting.")
        self._episode("watch locked", locked,
                      f"Job {job_name}: Windows is locked. The local Altium and AEDT steps need an unlocked "
                      "screen; unlock the PC (a failed job is resumed).")
        self._episode("watch modal", bool(titles),
                      f"Job {job_name}: Altium has a dialog open ({'; '.join(titles)}). Dismiss it; a failed "
                      "job is resumed.")
        self._episode("watch stop", self.stop_requested(),
                      f"STOP file found: {job_name} will finish, then the sweep exits.")

    def run_stage(self, record: dict) -> Optional[str]:
        if not record.get("run_id"):
            return None
        state = read_json(self.runs_root / record["run_id"] / "state.json") or {}
        return state.get("stage")

    def recover_run_id(self, record: dict) -> Optional[str]:
        if record.get("run_id"):
            return record["run_id"]
        found = RUN_ID_RE.search(read_text_tail(Path(record["log"]) if record.get("log") else None))
        return found.group(1) if found else None

    def evaluate(self, job_name: str, record: dict, finished: bool) -> List[dict]:
        if not record.get("run_id"):
            return []
        errors = None
        if record.get("log_offset") is not None:
            errors = self.desktop.worker_errors(record["log_offset"])
        return evaluate_run(self.runs_root, record["run_id"], errors, finished,
                            self.plan.name, job_name, time.strftime("%Y-%m-%d", time.localtime(self.clock())))

    def finish(self, job_name: str, record: dict, status: str, reason: str, rows: List[dict]) -> str:
        record["status"] = status
        record["reason"] = reason
        record["finished"] = iso(self.clock())
        record["combos"] = rows
        if status in ("done", "invalid"):
            record["log_offset"] = None
        self.save()
        render_results(self.folder, self.plan, self._all_rows(), self.clock())
        self.write_status(force=True)
        self.journal.info(f"Job {job_name}: {status}" + (f" -- {reason}" if reason else ""))
        if status in ("failed", "timeout", "invalid"):
            self.journal.alert(f"Job {job_name} is {status}: {reason}")
        return status

    def _all_rows(self) -> List[dict]:
        rows: List[dict] = []
        for name in self.state.get("order", []):
            rows.extend(self.state["jobs"].get(name, {}).get("combos", []))
        return rows

    def run_job(self, job: Job) -> str:
        record = self.state["jobs"][job.name]
        self.current_job = job.name
        while True:
            if not self.wait_for_previous_child(job.name, record):
                record["status"] = "pending"
                self.save()
                return "stopped"
            if not self.preflight(job.name):
                record["status"] = "pending"
                self.save()
                return "stopped"
            if not self.dry_run:
                self.close_foreign_solidworks(job.name)
            record["run_id"] = self.recover_run_id(record)
            record["attempts"] = record.get("attempts", 0) + 1
            now = self.clock()
            record.update(status="running", started=iso(now), started_epoch=now, finished=None)
            if record.get("log_offset") is None:
                record["log_offset"] = self.desktop.worker_log_size() or 0
            log_path = self.folder / "logs" / f"{job.name}.attempt{record['attempts']}.log"
            record["log"] = str(log_path)
            cmd = job_command(self.plan, job, record["run_id"])
            self.journal.info(f"Starting {job.name} (attempt {record['attempts']}): " + " ".join(cmd))
            self.save()
            self.write_status("running", force=True)
            proc = self.launch(cmd, log_path)
            record["child_pid"] = proc.pid
            record["child_create_time"] = self.desktop.create_time(proc.pid)
            self.save()

            outcome = self.supervise(proc, job.name, record, log_path)
            record["wall_seconds"] = round(record.get("wall_seconds", 0.0) + (self.clock() - now), 1)
            record["child_pid"] = None
            record["child_create_time"] = None
            if not record.get("run_id"):
                record["run_id"] = self.recover_run_id(record)
            if outcome.timed_out:
                reason = (f"wall-time limit of {self.plan.max_job_hours:g} h reached; its pipeline process was "
                          "terminated and the job resumes on the next start")
                rows = self.evaluate(job.name, record, finished=False)
                return self.finish(job.name, record, "timeout", reason, rows)

            rc = -1 if outcome.rc is None else outcome.rc
            stage = self.run_stage(record)
            tail = read_text_tail(log_path)
            environment = self._environment_cause(tail, outcome.blocked)
            finished_ok = rc == 0 and stage == "complete"
            rows: List[dict] = []
            verdict: Tuple[str, str] = ("failed", "")
            if finished_ok:
                rows = self.evaluate(job.name, record, finished=True)
                verdict = job_verdict(rows)
                if verdict[0] != "failed" or environment is None:
                    return self.finish(job.name, record, verdict[0], verdict[1], rows)
                # Combinations failed while the screen was locked or a dialog
                # was open: the pipeline went on without them. A resume redoes them.
                cause: Optional[str] = environment
            elif rc == 0 and stage == "geometry_ready":
                cause = GEOMETRY_CAUSE
            elif rc != 0 and environment is not None:
                cause = environment
            else:
                cause = None

            # A failure a resume cures goes back to the start of the job rather
            # than counting as a failure, at most MAX_INTERRUPTIONS times.
            if cause is not None and record.get("interruptions", 0) < MAX_INTERRUPTIONS:
                record["interruptions"] = record.get("interruptions", 0) + 1
                record.update(status="pending", reason=f"interrupted: {cause}; resuming the run")
                self.save()
                if cause == GEOMETRY_CAUSE:
                    self.journal.info(f"Job {job.name} interrupted ({cause}); resuming.")
                else:
                    self.journal.alert(f"Job {job.name} interrupted ({cause}); it resumes once the "
                                       "preflight passes.")
                continue

            if not rows:
                rows = self.evaluate(job.name, record, finished=False)
            if cause is not None:
                if finished_ok:
                    detail = verdict[1]
                elif rc != 0:
                    detail = f"exit code {rc}: {last_error_line(tail)}"
                else:
                    detail = f"the run ended at stage {stage!r}"
                reason = f"{cause} (gave up after {MAX_INTERRUPTIONS} resumes)" + (f"; {detail}" if detail else "")
            elif finished_ok:
                reason = verdict[1]
            elif rc == 0:
                reason = f"the run ended at stage {stage!r}, not complete"
            else:
                reason = f"exit code {rc}: {last_error_line(tail) or 'no error line in the log'}"
            return self.finish(job.name, record, "failed", reason, rows)

    @staticmethod
    def _environment_cause(tail: str, blocked: bool) -> Optional[str]:
        """Why a child failed when the cause is the desktop or a share, not the solve; None otherwise."""
        for needle, text_of_cause in TRANSIENT:
            if needle in tail:
                return text_of_cause
        if blocked:
            return "the screen was locked or an Altium dialog was open during the job"
        return None

    def run(self) -> int:
        self.load_state()
        self.journal.info(f"Sweep {self.plan.name} starting: {len(self.plan.jobs)} job(s), preset "
                          f"{self.plan.preset}, folder {self.folder}")
        if self.stop_requested():
            self.journal.info("STOP file present at start: exiting. Remove it to start the sweep.")
            self.write_status("stopped")
            return EXIT_OK
        self.write_status("starting", force=True)
        failures = False
        stopped = False
        for job in self.plan.jobs:
            record = self.state["jobs"][job.name]
            if record.get("status") in ("done", "invalid"):
                self.journal.info(f"Job {job.name}: already {record['status']}; skipped.")
                continue
            if self.stop_requested():
                stopped = True
                break
            outcome = self.run_job(job)
            self.current_job = None
            if outcome == "stopped":
                stopped = True
                break
            if outcome in ("failed", "timeout"):
                failures = True
                if self.stop_on_error:
                    self.journal.alert("Stopping after the failed job (--stop-on-error).")
                    break
        state_name = "stopped" if stopped else ("finished-with-failures" if failures else "finished")
        self.write_status(state_name, force=True)
        self.journal.info(f"Sweep {self.plan.name}: {state_name}.")
        return EXIT_FAILURES if failures else EXIT_OK


def new_job_record(job: Job) -> dict:
    return {
        "args": list(job.args), "status": "pending", "attempts": 0, "interruptions": 0,
        "run_id": None, "child_pid": None, "child_create_time": None, "log": None,
        "log_offset": None, "started": None, "started_epoch": None, "finished": None,
        "wall_seconds": 0.0, "reason": "", "combos": [],
    }


class _Journal:
    """runner.log for everything; alerts.txt for what needs a person."""

    def __init__(self, folder: Path, clock: Callable[[], float], echo: bool) -> None:
        self.folder = Path(folder)
        self.clock = clock
        self.echo = echo

    def info(self, text: str) -> None:
        self._write("runner.log", f"{stamp(self.clock())}  {text}")

    def alert(self, text: str) -> None:
        line = f"{stamp(self.clock())}  {text}"
        self._write("alerts.txt", line)
        self._write("runner.log", f"{stamp(self.clock())}  ALERT  {text}")

    def _write(self, name: str, line: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.folder / name, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self.echo:
            print(line, flush=True)


# ----------------------------------------------------------- dry run & CLI ----

def dry_run(plan: Plan, folder: Path, desktop, project_root: Path, runs_root: Path, out=print) -> int:
    """Print what a run would do and check the desktop. Launches, closes and writes nothing."""
    state = read_json(folder / "state.json") or {}
    out(f"Dry run of plan {plan.name} (preset {plan.preset}); nothing is started, closed or written.")
    out(f"Sweep folder: {folder}   max per job: {plan.max_job_hours:g} h")
    runner = SweepRunner(plan, folder, desktop=desktop, project_root=project_root, runs_root=runs_root,
                         echo=False, dry_run=True)
    problems = runner.check_problems()
    out("Preflight now: " + ("all clear" if not problems else "; ".join(sorted(problems.values()))))
    documents = desktop.solidworks_documents()
    for note in desktop.drain_notes():
        out(f"SolidWorks note: {note}")
    foreign = foreign_documents(documents, project_root, main_checkout_of(project_root))
    out(f"SolidWorks: {len(documents)} document(s) open, {len(foreign)} from another checkout"
        + (f" (would close, without saving: {', '.join(foreign)})" if foreign else ""))
    for index, job in enumerate(plan.jobs, 1):
        settings = job_settings(job.args, plan.preset)
        record = state.get("jobs", {}).get(job.name)
        if record and record.get("status") in ("done", "invalid"):
            decision = f"already {record['status']}: skipped"
        elif record and record.get("run_id"):
            decision = f"would resume run {record['run_id']} (status {record.get('status')})"
        else:
            decision = "would start a new run"
        out(f"[{index}] {job.name}: {decision}")
        out("    command: " + " ".join(job_command(plan, job, None)))
        out(f"    solver: preset {settings.get('solver_preset')}; " + solver_text(settings))
        overrides = settings.get("solver_overrides") or []
        if overrides:
            out(f"    WARNING: overrides the preset for {', '.join(overrides)}")
    return EXIT_OK


def acquire_lock(path: Path, desktop) -> Tuple[bool, Optional[dict]]:
    """One runner per sweep. Returns (acquired, the holder if not)."""
    holder = read_json(path)
    if holder and desktop.process_alive(holder.get("pid"), holder.get("create_time")):
        return False, holder
    me = psutil.Process()
    write_json_atomic(path, {"pid": os.getpid(), "create_time": me.create_time(),
                             "started": iso(time.time())})
    return True, None


def release_lock(path: Path) -> None:
    holder = read_json(path)
    if holder and holder.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a plan of solver sweeps unattended")
    parser.add_argument("plan", type=Path, help="the plan JSON, e.g. sweeps/examples/depth_converged.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands and run every check, without starting, closing or writing anything")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="stop after the first job that fails or times out (default: go on to the next job)")
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        print(f"Plan refused: {exc}", file=sys.stderr)
        return EXIT_PLAN
    root = os.environ.get("ANSYS_ANALYZE_QUEUE_PATH")
    if not root:
        print("ANSYS_ANALYZE_QUEUE_PATH is not set: the runner needs the worker's queue folder.", file=sys.stderr)
        return EXIT_PLAN
    folder = SWEEPS_ROOT / plan.name
    desktop = Desktop(Path(root))
    if args.dry_run:
        try:
            return dry_run(plan, folder, desktop, paths.PROJECT_ROOT, paths.RUNS_ROOT)
        except PlanError as exc:
            print(f"Plan refused: {exc}", file=sys.stderr)
            return EXIT_PLAN

    folder.mkdir(parents=True, exist_ok=True)
    if (folder / STOP_FILE).exists():
        print(f"{folder / STOP_FILE} is present: delete it to start the sweep.")
        return EXIT_OK
    lock = folder / "runner.lock"
    acquired, holder = acquire_lock(lock, desktop)
    if not acquired:
        print(f"Sweep {plan.name} is already running (pid {holder.get('pid')}); not starting a second runner.",
              file=sys.stderr)
        return EXIT_BUSY
    write_json_atomic(folder / "plan.json", dataclasses.asdict(plan))
    runner = SweepRunner(plan, folder, desktop=desktop, project_root=paths.PROJECT_ROOT,
                         runs_root=paths.RUNS_ROOT, stop_on_error=args.stop_on_error)
    try:
        return runner.run()
    except KeyboardInterrupt:
        runner.journal.info("Interrupted from the keyboard. A pipeline child in the same console stops on its own; "
                            "start the sweep again to resume.")
        return 130
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
