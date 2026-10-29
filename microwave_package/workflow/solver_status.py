"""
Whether HFSS converged, for each combination of a run.

HFSS writes a solver profile beside every solved project: a text file whose
footnote and basis-element lines say how the adaptive passes and each frequency
sweep ended. This module reads those lines, and the combination's own results
table (its S-parameters), and gives each combination one verdict:

    ok        the solve finished, and every sweep converged
    warning   the solve finished, with a note: the adaptive passes or the
              secondary criterion did not converge, or a converged sweep has a
              basis solution that is not passive
    failed    a sweep did not converge, the sweep simulation failed, the engine
              detected an error, or the solve did not finish; or a point of the
              S-parameters is above 0 dB or has more power out than in. The
              values between the solved frequencies are then unreliable.
    unknown   no profile yet, or a solve still going with no verdict

A combination's verdict is the worst of its profiles' verdicts, and a run's is
the worst of its own combinations' (unknown only when none has a verdict). A
refined solve and an eigenmode part are read the same way, from their own folders,
and shown on their own rows. The run's verdict counts its own combinations only: a
failed refined solve shows on its row and in the refine list, not on the run's
badge or banner, although its points can reach the run's plot.

Reading is kept cheap for a run list that asks every few seconds: a profile is
parsed once per version of its file (size and modification time), a run's
folder is looked at again only when its state.json changes, and a combination's
result is also saved beside its results (solver_status.json) so that a restarted
web app does not parse the whole archive again.
"""
from __future__ import annotations

import csv
import dataclasses
import glob
import json
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from .run_manager import Run

VERDICTS = ("unknown", "ok", "warning", "failed")
_RANK = {verdict: rank for rank, verdict in enumerate(VERDICTS)}

# Bump when a rule below changes: a result saved under the old rules is read again.
RULES = 1

# |S11|^2 + |S21|^2 (and |S12|^2 + |S22|^2) above this is more power out than in.
POWER_LIMIT = 1.02
DB_COLUMNS = ("s11_db", "s21_db", "s12_db", "s22_db")
PORT_PAIRS = (("s11_db", "s21_db"), ("s12_db", "s22_db"))
RESULTS_NAME = "results.csv"
CACHE_NAME = "solver_status.json"
PROFILE_GLOB = os.path.join("*.aedtresults", "*.results", "*.profile")

# The messages, as the page shows them.
DID_NOT_CONVERGE = "Interpolating sweep did NOT converge"
SIMULATION_FAILED = "Sweep simulation FAILED"
ENGINE_ERROR = "Engine detected an error"
DID_NOT_FINISH = "Solve did not finish (aborted)"
ADAPTIVE_NOT_CONVERGED = "Adaptive passes did not converge"
ADAPTIVE_NOT_CONSECUTIVE = "Adaptive passes converged, but not as many consecutive passes as requested"
SECONDARY_NOT_CONVERGED = "Secondary solver criterion is not converged"
NOT_PASSIVE = "Sweep converged, but a basis solution is not passive"
ADAPTIVE_CONVERGED = "Adaptive passes converged"
SWEEP_PASSIVE = "Sweep converged and is passive"
POINTS_BAD = "Some points are above 0 dB, or have more power out than in"
WARNING_LABELS = frozenset({ADAPTIVE_NOT_CONVERGED, ADAPTIVE_NOT_CONSECUTIVE, SECONDARY_NOT_CONVERGED, NOT_PASSIVE})

# The profile's lines are written with their quotes escaped (\'Status\', \'Aborted\'),
# so the backslashes are optional here.
_BASIS_NUMBER = re.compile(r"Basis Element # (\d+),")
_S_MATRIX_ERROR = re.compile(r"S Matrix Error\s*=\s*([\d.]+)%")
_STATUS = re.compile(r"Status\\?', \\?'([^'\\]+)")
_NOT_CONSECUTIVE = re.compile(r"adaptive passes converged, but \d+ consecutive")
_SWEEP_END = re.compile(r"Interpolating sweep (converged|did NOT converge)")


@dataclasses.dataclass(frozen=True)
class Reading:
    """What one profile says. The basis elements and the last S-matrix error are
    those of the sweep that decided the verdict: the first sweep that did not
    converge, else the last sweep that has any basis elements."""
    verdict: str
    messages: Tuple[str, ...] = ()
    sweeps: int = 0
    basis_elements: Optional[int] = None
    last_s_matrix_error: Optional[float] = None


class _Sweep:
    """One sweep's basis elements: the lines up to the sweep's own footnote.
    Basis numbering starts again at 1 in each sweep."""

    def __init__(self) -> None:
        self.numbers = set()
        self.error: Optional[float] = None
        self.failed = False

    def has_content(self) -> bool:
        return bool(self.numbers) or self.error is not None


def _footnote_notes(line: str) -> Tuple[List[Tuple[str, str]], bool]:
    """The (verdict, message) pairs one footnote line gives, and whether it says the
    solve completed normally."""
    notes = []
    if "did NOT converge" in line:
        notes.append(("failed", DID_NOT_CONVERGE))
    if "Sweep Simulation FAILED" in line:
        notes.append(("failed", SIMULATION_FAILED))
    # Case matters: "Adaptive Passes" is the footnote, "adaptive passes" the note.
    if "Adaptive Passes did not converge" in line:
        notes.append(("warning", ADAPTIVE_NOT_CONVERGED))
    if _NOT_CONSECUTIVE.search(line):
        notes.append(("warning", ADAPTIVE_NOT_CONSECUTIVE))
    elif "Adaptive Passes converged" in line:
        notes.append(("ok", ADAPTIVE_CONVERGED))
    if "Interpolating sweep converged and is passive" in line:
        notes.append(("ok", SWEEP_PASSIVE))
    if "non-passive basis solution" in line:
        notes.append(("warning", NOT_PASSIVE))
    completed = False
    status = _STATUS.search(line)
    if status:
        state = status.group(1)
        if state == "Normal Completion":
            completed = True
        elif state == "Engine Detected Error":
            notes.append(("failed", ENGINE_ERROR))
        elif state == "Aborted":
            notes.append(("failed", DID_NOT_FINISH))
        else:
            notes.append(("failed", f"Solve ended with status '{state}'"))
    return notes, completed


def read_profile(text: str) -> Reading:
    """The verdict and details of one solver profile, from its text."""
    notes: List[Tuple[str, str]] = []
    sweeps: List[_Sweep] = []
    current = _Sweep()
    completed = False
    for line in text.splitlines():
        if "Basis Element #" in line:
            number = _BASIS_NUMBER.search(line)
            if number:
                current.numbers.add(int(number.group(1)))
            error = _S_MATRIX_ERROR.search(line)
            if error:
                current.error = float(error.group(1))
            if SECONDARY_NOT_CONVERGED in line:
                notes.append(("warning", SECONDARY_NOT_CONVERGED))
        elif "ProfileFootnote" in line:
            end = _SWEEP_END.search(line)
            if end:
                current.failed = end.group(1) == "did NOT converge"
                sweeps.append(current)
                current = _Sweep()
            found, done = _footnote_notes(line)
            notes.extend(found)
            completed = completed or done
    if current.has_content():
        sweeps.append(current)

    failing = [sweep for sweep in sweeps if sweep.failed]
    with_content = [sweep for sweep in sweeps if sweep.has_content()]
    if failing:
        deciding = failing[0]
    elif with_content:
        deciding = with_content[-1]
    else:
        deciding = None

    seen = {verdict for verdict, _ in notes}
    if "failed" in seen:
        verdict = "failed"
    elif "warning" in seen:
        verdict = "warning"
    elif completed:
        verdict = "ok"
    else:
        verdict = "unknown"
    return Reading(
        verdict=verdict,
        messages=tuple(dict.fromkeys(label for _, label in notes)),
        sweeps=len(sweeps),
        basis_elements=len(deciding.numbers) if deciding is not None and deciding.numbers else None,
        last_s_matrix_error=deciding.error if deciding is not None else None,
    )


def read_profile_file(path: str) -> Reading:
    """`read_profile` of a profile file on disk."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return read_profile(handle.read())


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _power(db: float) -> float:
    """The power ratio a magnitude in dB stands for: |S|^2 = 10^(dB/10)."""
    try:
        return 10.0 ** (db / 10.0)
    except OverflowError:
        return math.inf


def read_results(path: str) -> Optional[Dict[str, Optional[int]]]:
    """How many points of a results table are above 0 dB, have more power out than
    in, or are either. A count is None when the table has no column for it (a
    launch study has no port 2), and the whole result is None when the table cannot
    be read."""
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            columns = [name for name in DB_COLUMNS if name in fields]
            pairs = [(a, b) for a, b in PORT_PAIRS if a in fields and b in fields]
            points = above = more = bad = 0
            for row in reader:
                points += 1
                values = {name: _number(row.get(name)) for name in columns}
                is_above = any(v is not None and v > 0.0 for v in values.values())
                is_more = any(
                    values[a] is not None and values[b] is not None
                    and _power(values[a]) + _power(values[b]) > POWER_LIMIT
                    for a, b in pairs)
                above += is_above
                more += is_more
                bad += is_above or is_more
    except (OSError, csv.Error, ValueError):
        return None
    return {
        "points": points,
        "above_0db": above if columns else None,
        "more_power_out": more if pairs else None,
        "bad": bad if (columns or pairs) else None,
    }


def worst(verdicts: Iterable[str]) -> str:
    """The most serious of some verdicts; "unknown" when none is known."""
    return max(verdicts, key=_RANK.get, default="unknown")


# -- the caches ---------------------------------------------------------------
# A file's version is its size and modification time. Each cache holds what was
# read for one version of a file, and is read again only when the version changes.
_profile_memo: Dict[str, Tuple[Tuple[int, int], Reading]] = {}
_results_memo: Dict[str, Tuple[Tuple[int, int], Optional[Dict[str, Optional[int]]]]] = {}
# run folder -> (state.json version, whether it was saved, the run's status)
_run_memo: Dict[str, Tuple[Tuple[int, int], bool, dict]] = {}


def clear_caches() -> None:
    """Forget every reading made so far. The saved files are left as they are."""
    _profile_memo.clear()
    _results_memo.clear()
    _run_memo.clear()


def _version(path: str) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


def _saved(combo_dir: str) -> dict:
    """A combination's solver_status.json, if it was saved under these rules."""
    try:
        with open(os.path.join(combo_dir, CACHE_NAME), encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) and saved.get("rules") == RULES else {}


def _reading_to_dict(reading: Reading) -> dict:
    return {"verdict": reading.verdict, "messages": list(reading.messages), "sweeps": reading.sweeps,
            "basis_elements": reading.basis_elements, "last_s_matrix_error": reading.last_s_matrix_error}


def _reading_from_dict(saved: dict) -> Reading:
    return Reading(verdict=saved["verdict"], messages=tuple(saved["messages"]), sweeps=saved["sweeps"],
                   basis_elements=saved["basis_elements"], last_s_matrix_error=saved["last_s_matrix_error"])


def _profile_reading(path: str, version: Tuple[int, int], saved: Optional[dict]) -> Reading:
    memo = _profile_memo.get(path)
    if memo is not None and memo[0] == version:
        return memo[1]
    reading = None
    if saved and (saved.get("size"), saved.get("mtime_ns")) == version:
        try:
            reading = _reading_from_dict(saved)
        except (KeyError, TypeError, ValueError):
            reading = None
    if reading is None:
        try:
            reading = read_profile_file(path)
        except OSError:
            reading = Reading(verdict="unknown")
    _profile_memo[path] = (version, reading)
    return reading


def _results_counts(path: str, version: Tuple[int, int], saved: Optional[dict]):
    memo = _results_memo.get(path)
    if memo is not None and memo[0] == version:
        return memo[1]
    if saved and (saved.get("size"), saved.get("mtime_ns")) == version and "counts" in saved:
        counts = saved["counts"]
    else:
        counts = read_results(path)
    _results_memo[path] = (version, counts)
    return counts


def combo_status(combo_dir: str, *, may_write: bool = False) -> Dict[str, object]:
    """One combination's verdict and what it rests on, from the profiles and the
    results table in its folder. With `may_write`, what was read is saved beside the
    results (solver_status.json) so the next start does not read the profiles again;
    a run that is still going is passed `may_write=False`, since its files are
    changing."""
    saved = _saved(combo_dir)
    saved_profiles = saved.get("profiles") or {}
    profiles: Dict[str, dict] = {}
    readings: List[Reading] = []
    for path in sorted(glob.glob(os.path.join(glob.escape(combo_dir), PROFILE_GLOB))):
        version = _version(path)
        if version is None:
            continue
        rel = os.path.relpath(path, combo_dir).replace(os.sep, "/")
        reading = _profile_reading(path, version, saved_profiles.get(rel))
        readings.append(reading)
        profiles[rel] = {"size": version[0], "mtime_ns": version[1], **_reading_to_dict(reading)}

    results_path = os.path.join(combo_dir, RESULTS_NAME)
    results_version = _version(results_path)
    counts = None
    results_saved = None
    if results_version is not None:
        results_saved = {"size": results_version[0], "mtime_ns": results_version[1],
                         "counts": _results_counts(results_path, results_version, saved.get("results"))}
        counts = results_saved["counts"]

    if may_write and (saved_profiles != profiles or saved.get("results") != results_saved):
        Run._write_json(os.path.join(combo_dir, CACHE_NAME),
                        {"rules": RULES, "profiles": profiles, **({"results": results_saved} if results_saved else {})})

    chosen: Optional[Reading] = None
    messages: List[str] = []
    for reading in readings:
        for label in reading.messages:
            if label not in messages:
                messages.append(label)
        # The first profile with the worst verdict is the one whose sweep is reported.
        if chosen is None or _RANK[reading.verdict] > _RANK[chosen.verdict]:
            chosen = reading
    verdict = chosen.verdict if chosen is not None else "unknown"
    bad = counts.get("bad") if counts else None
    if bad:
        verdict = "failed"
        messages.append(POINTS_BAD)
    return {
        "verdict": verdict,
        "messages": messages,
        "profiles": len(readings),
        "sweeps": chosen.sweeps if chosen is not None else 0,
        "basis_elements": chosen.basis_elements if chosen is not None else None,
        "last_s_matrix_error": chosen.last_s_matrix_error if chosen is not None else None,
        "points": counts.get("points") if counts else None,
        "points_above_0db": counts.get("above_0db") if counts else None,
        "points_more_power_out": counts.get("more_power_out") if counts else None,
        "points_bad": bad,
    }


def run_status(run_dir: str, *, may_write: bool = False) -> Dict[str, object]:
    """A run's verdict, and each of its combinations' (see `combo_status`). The
    combinations are the folders under models/, which are the ones built. A run's
    status is kept until its state.json changes, which is when its profiles and
    results change too."""
    state_version = _version(os.path.join(run_dir, "state.json"))
    memo = _run_memo.get(run_dir)
    if state_version is not None and memo is not None and memo[0] == state_version and (memo[1] or not may_write):
        return memo[2]
    models = os.path.join(run_dir, "models")
    try:
        names = sorted(entry.name for entry in os.scandir(models) if entry.is_dir())
    except OSError:
        names = []
    combos = {name: combo_status(os.path.join(models, name), may_write=may_write) for name in names}
    status = {"verdict": worst(combo["verdict"] for combo in combos.values()), "combos": combos}
    if state_version is not None:
        _run_memo[run_dir] = (state_version, may_write, status)
    return status
