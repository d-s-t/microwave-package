"""
Pairing an Eigenmode run with a Driven Modal run on the same geometry.

An HFSS design has exactly one solution type, so Eigenmode and Driven
Modal cannot share a setup -- and the queue's task names one project, one
design and one solution type, which rules out putting both designs in one
project for the worker too. Without changing that contract, the pair is
two ordinary runs over the same sweep:

  * the **Eigenmode run** goes first and is a run like any other;
  * the **Driven run** is linked to it by `eigen_run_id` in its config.
    It borrows the Eigenmode run's finished geometry (the STEP files and
    the copper, so Altium and SolidWorks are not asked again) and reads
    its modes to put fine frequency windows around each one (see
    hfss/driven/windows.py).

Each run records the other (`eigen_run_id` / `driven_run_id`), which is all
that makes them a pair: nothing else treats them differently, so every
existing page, resume and delete works on each as it did.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import shutil
from typing import Callable, Dict, List, Optional, Tuple

from ..hfss import registry
from ..hfss.driven import DRIVEN_MODAL
from ..hfss.eigenmode import EIGENMODE
from ..design.sweep import SweepParameter
from .run_manager import Run

logger = logging.getLogger("microwave_package")

EIGEN_LINK = "eigen_run_id"
DRIVEN_LINK = "driven_run_id"
# A refinement is a driven run that re-solves chosen combinations of
# another driven run over narrower frequency ranges.
REFINES_LINK = "refines_run_id"
REFINED_BY = "refined_by"
# A field-image part is a driven run that solves chosen combinations at one
# frequency, to draw their fields (see create_field_images). Its parent lists
# it under FIELD_PARTS; the part itself names its parent as `part_of`.
FIELD_PARTS = "field_image_parts"

# What a combination's geometry is, among the files in its folder. The
# rest -- the AEDT project, the solved results -- belong to the solve and
# must not be copied into another run.
_GEOMETRY_FILES = ("dimensions.txt", "copper.txt", "bonds.txt")


def create_pair(sweep_params, analysis_settings: dict, parameter_defaults=None) -> Tuple[Run, Run]:
    """Create the Driven run the user asked for, and the Eigenmode run behind it.

    Returns `(eigenmode_run, driven_run)`. The Driven run is the one that
    is listed and has the page; the Eigenmode run is a hidden part of it
    (`part_of`), whose progress and results are shown there. They carry the
    same sweep and the same settings except for the solution type.
    """
    driven = Run.create(sweep_params, {**analysis_settings, "solution_type": DRIVEN_MODAL},
                        parameter_defaults)
    eigen = Run.create(sweep_params, {**analysis_settings, "solution_type": EIGENMODE},
                       parameter_defaults, part_of=driven, role="eigen")
    eigen.config[DRIVEN_LINK] = driven.run_id
    driven.config[EIGEN_LINK] = eigen.run_id
    eigen.save_config()
    driven.save_config()
    return eigen, driven


def parts_of(run: Run) -> List[Run]:
    """The hidden runs that belong to this one: its eigenmode run, its
    refinements and its field-image parts. What deleting the run deletes along
    with it."""
    found = []
    for run_id in [run.config.get(EIGEN_LINK), *(run.config.get(REFINED_BY) or []),
                   *(run.config.get(FIELD_PARTS) or [])]:
        if not run_id:
            continue
        try:
            found.append(Run.load(run_id))
        except (OSError, ValueError, KeyError):
            continue
    return found


def prerequisite_of(run: Run) -> Optional[Run]:
    """The Eigenmode run this one has to follow, or None."""
    run_id = run.config.get(EIGEN_LINK)
    if not run_id:
        return None
    try:
        return Run.load(run_id)
    except (OSError, ValueError, KeyError):
        logger.warning("Run %s is paired with %s, which cannot be loaded.", run.run_id, run_id)
        return None


def refined_parent(run: Run) -> Optional[Run]:
    """The driven run this one refines, or None."""
    run_id = run.config.get(REFINES_LINK)
    if not run_id:
        return None
    try:
        return Run.load(run_id)
    except (OSError, ValueError, KeyError):
        logger.warning("Run %s refines %s, which cannot be loaded.", run.run_id, run_id)
        return None


def refinements_of(run: Run) -> List[Run]:
    """The refinement runs made from this one, oldest first, skipping any
    that have since been deleted."""
    found = []
    for run_id in run.config.get(REFINED_BY) or []:
        try:
            found.append(Run.load(run_id))
        except (OSError, ValueError, KeyError):
            continue
    return found


def refinable_combos(run: Run) -> List[str]:
    """The combinations of a driven run that have S-parameters to refine."""
    return [cid for cid, combo in run.state["combos"].items() if combo.get("post_status") == "done"]


def create_refinement(parent: Run, ranges_text: str, combo_ids: List[str],
                      points_cap: int = 20000) -> Run:
    """A new driven run that re-solves `combo_ids` of `parent` over `ranges_text`.

    `ranges_text` is the `lo:hi:step; ...` list the fine windows take (see
    hfss/driven/windows.py) -- as many ranges as wanted. The new run:

      * has the same geometry (it borrows the parent's, see `seed_geometry`)
        and the same settings and materials, so it is the same device;
      * solves ONLY those windows -- no broad sweep, no eigenmode windows;
      * adapts its mesh at the frequency the parent adapted at, so the
        fine data lines up with the broad data in one trace;
      * is linked both ways (`refines_run_id` / `refined_by`), which is
        what lets the parent's page show broad, fine, or both together.

    Raises ValueError for anything that cannot be refined, with the reason.
    """
    from ..hfss.driven import windows

    settings = parent.analysis_settings
    if registry.solution_type_of(settings) != DRIVEN_MODAL:
        raise ValueError("Only a Driven Modal run has a frequency range to refine.")
    wanted = list(dict.fromkeys(combo_ids))
    available = set(refinable_combos(parent))
    missing = [c for c in wanted if c not in available]
    if not wanted:
        raise ValueError("Pick at least one combination to refine.")
    if missing:
        raise ValueError(f"These combinations have no results to refine yet: {', '.join(missing)}.")
    parsed = windows.parse_manual(ranges_text)
    if not parsed:
        raise ValueError("Give at least one frequency range, as lo:hi:step in GHz.")

    # Where the parent meshed: the top of its band unless it said otherwise.
    parent_mesh = windows.mesh_frequency_ghz(settings)
    child_settings = {
        **settings,
        "coarse_sweep": "off",
        "fine_windows": "off",
        "fine_windows_manual": "; ".join(f"{w.lo:g}:{w.hi:g}:{w.step:g}" for w in parsed),
        "f_start_ghz": min(w.lo for w in parsed),
        "f_stop_ghz": max(w.hi for w in parsed),
        "adapt_ghz": parent_mesh,
        "fine_max_points": points_cap,
    }
    sweep_params = [SweepParameter(name=p["name"], values=p["values"])
                    for p in parent.config.get("sweep_params", [])]
    child = Run.create(sweep_params, child_settings, parent.parameter_defaults,
                       part_of=parent, role=f"fine{len(parent.config.get(REFINED_BY) or []) + 1}")
    keep = set(wanted)
    child.state["combos"] = {c: v for c, v in child.state["combos"].items() if c in keep}
    child.config[REFINES_LINK] = parent.run_id
    child.save()
    parent.config[REFINED_BY] = [*parent.config.get(REFINED_BY, []), child.run_id]
    parent.save_config()
    return child


def field_parent(run: Run) -> Optional[Run]:
    """The driven run a field-image part draws its fields from, or None."""
    if run.analysis_settings.get("field_frequency_ghz") is None:
        return None
    run_id = run.config.get("part_of")
    if not run_id:
        return None
    try:
        return Run.load(run_id)
    except (OSError, ValueError, KeyError):
        logger.warning("Run %s draws its fields from %s, which cannot be loaded.", run.run_id, run_id)
        return None


def field_image_parts(run: Run) -> List[Run]:
    """The field-image parts made from this run, oldest first, skipping any
    that have since been deleted."""
    found = []
    for run_id in run.config.get(FIELD_PARTS) or []:
        try:
            found.append(Run.load(run_id))
        except (OSError, ValueError, KeyError):
            continue
    return found


def create_field_images(parent: Run, frequency_text: str, combo_ids: List[str],
                        quantities: List[str]) -> Run:
    """A new part of `parent` that solves `combo_ids` at one frequency, to draw their fields.

    `frequency_text` is the frequency in GHz and must lie inside the parent's
    band. `quantities` is any of "E" (|E|) and "H" (|H|). The new part:

      * has the same geometry (it borrows the parent's, see `seed_geometry`)
        and the same settings and materials, so it is the same device;
      * solves ONE adaptive frequency and no sweep (see hfss/driven/setup.py),
        meshed at that frequency, so its fields there are as good as the run
        can give;
      * is linked to the parent (`part_of`, and the parent's FIELD_PARTS), so
        it is listed under the parent's row and goes with it.

    Raises ValueError for anything that cannot be done, with the reason.
    """
    settings = parent.analysis_settings
    if registry.solution_type_of(settings) != DRIVEN_MODAL:
        raise ValueError("Field images need a Driven Modal run: only it has fields at a frequency to draw.")
    wanted = list(dict.fromkeys(combo_ids))
    if not wanted:
        raise ValueError("Pick at least one combination.")
    available = set(refinable_combos(parent))
    missing = [c for c in wanted if c not in available]
    if missing:
        raise ValueError(f"These combinations have no results yet: {', '.join(missing)}.")
    low, high = float(settings["f_start_ghz"]), float(settings["f_stop_ghz"])
    try:
        frequency = float(str(frequency_text).strip())
    except ValueError:
        raise ValueError("Give the frequency in GHz, as a number.") from None
    if not (math.isfinite(frequency) and low <= frequency <= high):
        raise ValueError(f"The frequency must lie inside the run's band, {low:g} to {high:g} GHz.")
    chosen = list(dict.fromkeys(q.strip().upper() for q in quantities))
    if not chosen or any(q not in ("E", "H") for q in chosen):
        raise ValueError("Tick at least one of |E| and |H|.")

    child_settings = {
        **settings,
        "coarse_sweep": "off",
        "fine_windows": "off",
        "fine_windows_manual": "",
        "f_start_ghz": frequency,
        "f_stop_ghz": frequency,
        "adapt_ghz": frequency,
        "field_frequency_ghz": frequency,
        "field_quantities": chosen,
    }
    sweep_params = [SweepParameter(name=p["name"], values=p["values"])
                    for p in parent.config.get("sweep_params", [])]
    child = Run.create(sweep_params, child_settings, parent.parameter_defaults,
                       part_of=parent, role=f"field{len(parent.config.get(FIELD_PARTS) or []) + 1}")
    keep = set(wanted)
    child.state["combos"] = {c: v for c, v in child.state["combos"].items() if c in keep}
    child.save()
    parent.config[FIELD_PARTS] = [*parent.config.get(FIELD_PARTS, []), child.run_id]
    parent.save_config()
    return child


def partner_id(run: Run) -> Optional[str]:
    return run.config.get(EIGEN_LINK) or run.config.get(DRIVEN_LINK)


def seed_geometry(run: Run, source: Run, log: Callable[[str], None] = logger.info) -> int:
    """Give `run` the geometry `source` already built, so it is not built twice.

    Per combination, and only where `run` has none of its own yet:
    copies the dimensions snapshot, the copper and the bond list, copies
    the STEP files, and records the Altium and SolidWorks stages as done
    -- which is the whole of what those two stages would have produced,
    and what the AEDT stage reads. Returns how many combinations it seeded.
    """
    seeded = 0
    for combo_id, combo in run.state["combos"].items():
        theirs = source.state["combos"].get(combo_id)
        if combo.get("skip_reason") or not theirs or theirs.get("step_status") != "done":
            continue
        if combo.get("step_status") == "done" and combo.get("step_paths"):
            continue
        source_dir, target_dir = source.combo_path(combo_id), run.combo_dir(combo_id)
        step_paths = {}
        try:
            for key, path in (theirs.get("step_paths") or {}).items():
                if not os.path.isfile(path):
                    raise FileNotFoundError(path)
                target = os.path.join(target_dir, os.path.basename(path))
                shutil.copyfile(path, target)
                step_paths[key] = target
            if not step_paths or not os.path.isfile(os.path.join(source_dir, "dimensions.txt")):
                continue
            for name in _GEOMETRY_FILES:
                if os.path.isfile(os.path.join(source_dir, name)):
                    shutil.copyfile(os.path.join(source_dir, name), os.path.join(target_dir, name))
        except OSError as exc:
            log(f"{combo_id}: could not reuse {source.run_id}'s geometry ({exc}); it will be rebuilt.")
            continue
        fields = {"step_paths": step_paths, "step_status": "done", "step_error": None,
                  "geometry_reused_from": source.run_id}
        if theirs.get("altium_status") == "done" and os.path.isfile(os.path.join(target_dir, "copper.txt")):
            fields.update(altium_status="done", altium_error=None, altium_reused_from=source.run_id)
        run.update_combo(combo_id, **fields)
        seeded += 1
    if seeded:
        log(f"Reused the geometry of {seeded} combination(s) from {source.run_id} -- "
            "neither Altium nor SolidWorks is asked for it again.")
    return seeded


def _as_float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if not math.isnan(number) else None


def modes_of(run: Run, combo_id: str) -> List[Tuple[float, Optional[float]]]:
    """`(freq_ghz, q)` for each eigenmode a run found for one combination.

    Read from the combination's `results.csv`, falling back to the
    post-processing result it was made from. `q` is None when the file has
    none. An empty list is a combination with no readable modes.
    """
    folder = run.combo_path(combo_id)
    rows: List[dict] = []
    csv_path = os.path.join(folder, "results.csv")
    if os.path.isfile(csv_path):
        try:
            with open(csv_path, newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:
            rows = []
    if not rows:
        try:
            with open(os.path.join(folder, "client_result.json"), encoding="utf-8") as handle:
                result = json.load(handle).get("result", {})
            rows = result.get("rows") or result.get("modes") or []
        except (OSError, ValueError):
            rows = []
    modes = []
    for row in rows:
        f = _as_float(row.get("freq_ghz"))
        if f is not None and f > 0:
            modes.append((f, _as_float(row.get("q"))))
    return sorted(modes)


def eigenmodes_for(run: Run, combo_id: str) -> List[Tuple[float, Optional[float]]]:
    """The modes of this combination's geometry, from the paired Eigenmode
    run -- empty when there is no pair, or that combination has no modes."""
    other = prerequisite_of(run)
    if other is None:
        return []
    return modes_of(other, combo_id)


def settings_with_modes(run: Run, combo_id: str, analysis_settings: dict,
                        log: Callable[[str], None] = logger.info) -> dict:
    """`analysis_settings` for one combination's driven setup: the same,
    with that combination's eigenmodes added under `eigenmodes`."""
    if registry.solution_type_of(analysis_settings) != DRIVEN_MODAL:
        return analysis_settings
    modes = eigenmodes_for(run, combo_id)
    if prerequisite_of(run) is not None:
        if modes:
            log(f"{combo_id}: {len(modes)} eigenmode(s) from {run.config[EIGEN_LINK]} "
                "for the fine windows.")
        else:
            log(f"{combo_id}: no eigenmodes found in {run.config[EIGEN_LINK]} -- "
                "the sweep gets no automatic fine windows.")
    return {**analysis_settings, "eigenmodes": modes}


def mode_markers(run: Run, combo_ids=None) -> List[Dict[str, object]]:
    """Every eigenmode of the paired Eigenmode run as `{combo_id, freq_ghz, q}`,
    for drawing on a driven run's S-parameter plot. `[]` for an unpaired run."""
    other = prerequisite_of(run)
    if other is None:
        return []
    wanted = set(combo_ids) if combo_ids is not None else None
    markers = []
    for combo_id in other.state.get("combos", {}):
        if wanted is not None and combo_id not in wanted:
            continue
        for f, q in modes_of(other, combo_id):
            markers.append({"combo_id": combo_id, "freq_ghz": f, "q": q})
    return markers


def mode_ranges(run: Run, combo_ids, lo_ghz: Optional[float] = None,
                hi_ghz: Optional[float] = None) -> str:
    """The `lo:hi:step; ...` text for refining around the eigenmodes of
    `combo_ids` that fall between `lo_ghz` and `hi_ghz` (the whole band by
    default), using the run's own window settings but sampling twice as
    densely as the automatic windows do -- the point of refining.

    Several combinations' windows are merged into one list: a refinement
    solves one set of ranges for every combination it is given. Empty when
    no mode falls in the range.
    """
    from ..hfss.driven import windows

    settings = run.analysis_settings
    f_start = float(settings.get("f_start_ghz", 0.0))
    f_stop = float(settings.get("f_stop_ghz", 0.0))
    found = []
    for combo_id in combo_ids:
        found += [(f, q) for f, q in eigenmodes_for(run, combo_id)
                  if (lo_ghz is None or f >= lo_ghz) and (hi_ghz is None or f <= hi_ghz)]
    if not found:
        return ""
    planned = windows.merge(windows.windows_from_modes(
        found, f_start, f_stop,
        halfwidth_linewidths=float(settings.get("fine_halfwidth_linewidths", 0.5)),
        points_per_linewidth=2 * float(settings.get("fine_points_per_linewidth", 20.0)),
        margin_percent=float(settings.get("fine_margin_percent", 1.0)),
        fallback_q=float(settings.get("fine_q_fallback", 200.0)),
    ))
    return "; ".join(f"{w.lo:.4f}:{w.hi:.4f}:{w.step:.5f}" for w in planned)


def eigenmode_table(run: Run) -> List[Dict[str, object]]:
    """Every eigenmode of the paired Eigenmode run as `{combo_id, mode, freq_ghz, q}`,
    for the run page's table. `[]` for a run with no eigenmode part."""
    other = prerequisite_of(run)
    if other is None:
        return []
    rows = []
    for combo_id in other.state.get("combos", {}):
        for index, (f, q) in enumerate(modes_of(other, combo_id), start=1):
            rows.append({"combo_id": combo_id, "mode": index, "freq_ghz": f, "q": q})
    return rows
