"""Saved comparisons: a named set of runs with the plot options and the
names this comparison gives its runs, one JSON file each under
`paths.SAVED_COMPARES_DIR`.

The names belong to the comparison alone -- a run's own name is never
touched, and the same run can be in two comparisons under two names.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Dict, List, Optional

from .. import paths

_ID = re.compile(r"cmp_[0-9a-f]{12}")
MAX_NAME = 120
_last_stamp = 0.0


def _stamp() -> float:
    """The time, bumped so two saves are never the same instant (the clock
    ticks in ~15 ms steps on Windows) and 'oldest first' stays well defined."""
    global _last_stamp
    _last_stamp = max(time.time(), _last_stamp + 1e-6)
    return _last_stamp


def _dir() -> str:
    return str(paths.SAVED_COMPARES_DIR)


def _file(compare_id: str) -> str:
    if not _ID.fullmatch(compare_id or ""):
        raise KeyError(compare_id)
    return os.path.join(_dir(), compare_id + ".json")


def _clean(record: dict) -> dict:
    """Only what a saved comparison holds, in the shapes it holds them."""
    runs = [str(r) for r in record.get("runs", []) if re.fullmatch(r"[A-Za-z0-9_\-]+", str(r))]
    names = {str(k): str(v).strip()[:MAX_NAME] for k, v in (record.get("names") or {}).items()
             if str(k) in runs and str(v).strip()}
    options = {str(k): v for k, v in (record.get("options") or {}).items()
               if isinstance(v, (str, dict))}
    return {"runs": runs, "names": names, "options": options}


def get(compare_id: str) -> Optional[dict]:
    try:
        with open(_file(compare_id), encoding="utf-8") as handle:
            return json.load(handle)
    except (KeyError, OSError, ValueError):
        return None


def listing() -> List[dict]:
    """Every saved comparison, newest first."""
    found = []
    try:
        files = os.listdir(_dir())
    except OSError:
        return []
    for name in files:
        record = get(name[:-5]) if name.endswith(".json") else None
        if record:
            found.append(record)
    return sorted(found, key=lambda r: r.get("saved_at", 0), reverse=True)


def save(name: str, record: dict, compare_id: Optional[str] = None) -> dict:
    """Create a saved comparison, or replace the one `compare_id` names
    (keeping its id, so links to it stay good)."""
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        raise ValueError("A saved comparison needs a name.")
    if compare_id is not None and get(compare_id) is None:
        compare_id = None
    saved = {"id": compare_id or "cmp_" + uuid.uuid4().hex[:12], "name": name,
             "saved_at": _stamp(), **_clean(record)}
    os.makedirs(_dir(), exist_ok=True)
    _write(saved)
    return saved


def _write(saved: dict) -> None:
    path = _file(saved["id"])
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(saved, handle, indent=1)
    os.replace(path + ".tmp", path)


def rename(compare_id: str, name: str) -> Optional[dict]:
    record = get(compare_id)
    name = (name or "").strip()[:MAX_NAME]
    if record is None or not name:
        return None
    record["name"] = name
    _write(record)
    return record


def delete(compare_id: str) -> bool:
    try:
        os.remove(_file(compare_id))
        return True
    except (KeyError, OSError):
        return False


def tags_by_run(records: Optional[List[dict]] = None) -> Dict[str, List[dict]]:
    """Run id -> the saved comparisons it is part of (`id`, `name`),
    oldest save first, for the tags on the run list and the run page."""
    tags: Dict[str, List[dict]] = {}
    for record in sorted(records if records is not None else listing(), key=lambda r: r.get("saved_at", 0)):
        for run_id in record.get("runs", []):
            tags.setdefault(run_id, []).append({"id": record["id"], "name": record["name"]})
    return tags
