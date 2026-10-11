"""
The box and lid drawings as saved SolidWorks drawings that follow the part.

`solidworks_drawing` builds a drawing from scratch every time. That is
reliable but it is also something nobody can touch: a draughtsman who wants
a dimension moved, a note reworded or a view re-laid has to change the
generator. So the generator is run once, here, to make `base_parts/box.SLDDRW`
and `base_parts/lid_inside.SLDDRW` -- real drawings, to be tuned by hand in
SolidWorks and committed -- and a combination's drawing is made from that:

    1. copy the template next to the combination's SLDPRT copy,
    2. point it at that copy (ReplaceReferencedDocument) and rebuild,
    3. refit the views to the sheet, for this board's size,
    4. check every dimension the generator placed against the value it must
       have, and hide the ones that do not apply here (no screws, no recess,
       no vents) or that landed on the wrong edge -- never print them; they
       are listed in a note instead,
    5. write the PDF.

A `<name>.layout.json` kept beside each template says which view and which
dimension the generator made, by name, so the drawing can be read back
without guessing; dimensions a person adds by hand are not in it and are
left alone.

Where the template cannot follow the part -- a view that does not exist in
it, or a section line / detail circle that has to move (a different screw
position, a different port space) -- `usable` says so and the caller builds
the drawing from scratch instead, which is also what happens when there is no
template at all.

Needs SolidWorks; everything that decides *what* to do (`usable`,
`classify_dims`, the sidecar) is plain data and is tested without it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from .. import paths
from . import equations as eq
from . import solidworks_drawing as sd
from . import step_generation

logger = logging.getLogger("microwave_package")

TEMPLATE_FILES = {"box": "box.SLDDRW", "lid_inside": "lid_inside.SLDDRW"}
TOLERANCE_MM = 0.01          # a dimension within this of the value it must have is right
CUT_TOLERANCE_MM = 0.01      # a section line or detail circle this close to where it was is still right


# -- where the templates live ------------------------------------------------

def template_path(part: str) -> str:
    return str(paths.BASE_PARTS_DIR / TEMPLATE_FILES[part])


def sidecar_path(part: str) -> str:
    return os.path.splitext(template_path(part))[0] + ".layout.json"


def load_sidecar(part: str, path: Optional[str] = None) -> Optional[dict]:
    """The sidecar for `part`, or None when there is none or it cannot be read."""
    try:
        with open(path or sidecar_path(part), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "views" in data and "dims" in data else None


def has_template(part: str) -> bool:
    return os.path.isfile(template_path(part)) and load_sidecar(part) is not None


def key_of(view: str, dim_id: str) -> str:
    return f"{view}|{dim_id}"


def make_sidecar(part: str, reference: str, layout: List[sd.View], view_names: Dict[str, str],
                 placed: Dict[Tuple[str, str], str]) -> dict:
    """What the generator made, by name, for the drawing it just built.

    `view_names` maps a layout view's key to its name in the drawing;
    `placed` maps (view key, dimension id) to the dimension's name there.
    """
    dims = {}
    for view in layout:
        for dim in view.dims:
            name = placed.get((view.key, dim.id))
            if name:
                dims[key_of(view.key, dim.id)] = {"name": name, "expected": dim.expected}
    return {
        "part": part,
        "reference": os.path.basename(reference),     # a file name, not this machine's path
        "views": {
            view.key: {
                "name": view_names.get(view.key),
                "scale": view.scale, "x": view.x, "y": view.y,
                "section_of": view.section_of, "section_at": view.section_at, "section_axis": view.section_axis,
                "detail_of": view.detail_of, "detail_at": list(view.detail_at),
                "detail_radius": view.detail_radius,
            }
            for view in layout
        },
        "dims": dims,
    }


def save_sidecar(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")


# -- deciding what the template can do (pure) ------------------------------

def usable(layout: List[sd.View], sidecar: dict) -> Tuple[bool, str]:
    """Whether the template can be made to follow this combination, or the
    drawing has to be built from scratch, and why."""
    views = sidecar.get("views", {})
    for view in layout:
        recorded = views.get(view.key)
        if recorded is None or not recorded.get("name"):
            return False, f"the template has no {view.key} view"
        if view.section_of:
            if recorded.get("section_axis") != view.section_axis:
                return False, f"{view.key}: the cut runs the other way in the template"
            if abs(float(recorded.get("section_at", 0.0)) - view.section_at) > CUT_TOLERANCE_MM:
                return False, (f"{view.key}: the cut line is at {recorded.get('section_at')} in the template "
                               f"and {view.section_at:g} here")
        if view.detail_of:
            at = recorded.get("detail_at", [0.0, 0.0])
            if (abs(float(at[0]) - view.detail_at[0]) > CUT_TOLERANCE_MM
                    or abs(float(at[1]) - view.detail_at[1]) > CUT_TOLERANCE_MM
                    or abs(float(recorded.get("detail_radius", 0.0)) - view.detail_radius) > CUT_TOLERANCE_MM):
                return False, f"{view.key}: the detail circle has to move"
    return True, ""


def classify_dims(layout: List[sd.View], sidecar: dict) -> Dict[str, object]:
    """Sort the template's generated dimensions for one combination.

    `check`   {sidecar key: expected value or None}: dimensions this
              combination has -- read each and compare it with the value
    `absent`  sidecar keys the generator would not make here (the feature
              is not in this combination): hide, and list
    `views_gone` view keys in the template that this combination has no use
              for (the vents' section, the screws' section): remove
    """
    wanted = {key_of(view.key, dim.id): dim.expected for view in layout for dim in view.dims}
    dims = sidecar.get("dims", {})
    check = {key: expected for key, expected in wanted.items() if key in dims}
    absent = sorted(key for key in dims if key not in wanted)
    live = {view.key for view in layout}
    views_gone = sorted(key for key in sidecar.get("views", {}) if key not in live)
    # a dimension of a view that is going is not "absent" separately
    absent = [key for key in absent if key.split("|", 1)[0] not in views_gone]
    return {"check": check, "absent": absent, "views_gone": views_gone}


def is_right(measured: float, expected: Optional[float]) -> bool:
    return expected is None or abs(measured - expected) <= max(TOLERANCE_MM, 0.002 * abs(expected))


def omitted_lines(items: List[str], width: int = 110, lines: int = sd.OMITTED_LINES) -> List[str]:
    """The note listing what was left off, as at most `lines` lines of at
    most `width` characters, or [] when nothing was. A list too long for
    them ends in "...": the log has all of it."""
    if not items:
        return []
    words = ("NOT SHOWN FOR THIS COMBINATION: " + "; ".join(items)).split(" ")
    out: List[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        if len(trial) <= width or not current:
            current = trial
        else:
            out.append(current)
            current = word
    out.append(current)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: width - 3].rstrip() + "..."
    return out


def describe(key: str, why: str) -> str:
    view, dim = key.split("|", 1)
    return f"{dim.split(':', 1)[-1]} on {view} ({why})"


# -- building the templates (SolidWorks) -----------------------------------

def build_template(app, part: str, log: Callable[[str], None] = logger.info) -> List[str]:
    """Run the generator once, on the base part with `default.txt`'s
    values, and save the result as `base_parts/<part>.SLDDRW` with its
    sidecar. Returns the dimensions the generator had to leave off (none, on
    a default board that has every feature).

    The drawing is saved against a copy of the part with the default
    equations applied (the base part may be linked to any checkout's
    equations file) and then pointed at the base part itself, so that
    opening the template in SolidWorks shows the real part.
    """
    base = str(paths.SOLIDWORKS_PART_FILES[part])
    values = eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))
    work = tempfile.mkdtemp(prefix="drawing_template_")
    copy = os.path.join(work, f"{part}_template_source.SLDPRT")
    from . import solidworks_manufacturing as manufacturing

    manufacturing._make_copy(app, part, base, str(paths.DEFAULT_EQUATIONS_FILE), copy, log)
    import pythoncom
    import win32com.client

    errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    model = app.OpenDoc6(copy, 1, 1, "", errors, warnings)
    if model is None:
        raise RuntimeError(f"could not open {copy} (SolidWorks error {errors.value})")
    target = template_path(part)
    try:
        drawing, sheet, layout = sd.build_drawing(app, model, copy, part, "TEMPLATE", values, log,
                                                  material="aluminium")
        names = {key: view.Name for key, view in sheet.sw_views.items()}
        try:
            ok, reason = step_generation.save_as_step(drawing, target, 1)
            if not ok:
                raise RuntimeError(f"could not save {target}: {reason}")
        finally:
            try:
                app.CloseDoc(drawing.GetTitle)
            except Exception:
                pass
        rejected = list(sheet.rejected)
    finally:
        try:
            app.CloseDoc(os.path.basename(copy))
        except Exception:
            pass
    if not app.ReplaceReferencedDocument(target, copy, base):
        raise RuntimeError(f"could not point {target} at {base}")
    save_sidecar(make_sidecar(part, base, layout, names, sheet.placed), sidecar_path(part))
    shutil.rmtree(work, ignore_errors=True)
    log(f"{part}: template saved to {target}")
    return rejected


# -- filling a template for one combination (SolidWorks) ---------------------

def short_name(full: str) -> str:
    """A dimension's name without the document it is in -- "RD2@Drawing
    View2" of "RD2@Drawing View2@Draw1.Drawing" -- since a drawing is
    called something else once it is saved under a new name."""
    return "@".join(str(full).split("@")[:2])


def _views_by_name(drawing) -> Dict[str, object]:
    found = {}
    view = drawing.GetFirstView
    view = view.GetNextView if view is not None else None      # the first one is the sheet itself
    while view is not None:
        found[view.Name] = view
        view = view.GetNextView
    return found


def _dimensions_by_name(views: Dict[str, object]) -> Dict[str, object]:
    found = {}
    for view in views.values():
        try:
            shown = view.GetDisplayDimensions
        except Exception:
            shown = None
        for display in (shown or []):
            try:
                found[short_name(display.GetDimension2(0).FullName)] = display
            except Exception:
                continue
    return found


def _hide(drawing, display, none) -> bool:
    """Take a dimension off this combination's drawing. Deleted: a dimension
    only hidden still shows when the drawing is opened to be tuned, and
    this copy is made for this combination alone, so nothing is lost -- the
    template keeps the dimension. Selected by its full name, which is what
    SolidWorks will select a dimension by (selecting its annotation does
    nothing); hidden if it will not be deleted."""
    try:
        full = display.GetDimension2(0).FullName
        drawing.ClearSelection2(True)
        if drawing.Extension.SelectByID2(full, "DIMENSION", 0, 0, 0, False, 0, none, 0):
            drawing.EditDelete()
            return True
    except Exception:
        pass
    try:
        annotation = display.GetAnnotation
        annotation.Visible = 0
        return True
    except Exception:
        return False


def _delete_cut_line(drawing, views: Dict[str, object], sidecar: dict, recorded: dict, none) -> None:
    """Take the cutting line (and its arrows) of a section that has gone off
    the sheet, or the plan view is left showing a cut to nowhere. Found
    where the line is: in the parent view, at the cut's offset along it."""
    parent = views.get(sidecar["views"].get(recorded["section_of"], {}).get("name"))
    if parent is None:
        return
    try:
        x0, y0 = parent.Position
        offset = float(parent.ScaleDecimal) * float(recorded.get("section_at", 0.0)) / 1000.0
        x, y = (x0 + offset, y0) if recorded.get("section_axis") == "v" else (x0, y0 + offset)
        drawing.ClearSelection2(True)
        if drawing.Extension.SelectByID2("", "SECTIONLINE", x, y, 0, False, 0, none, 0):
            drawing.EditDelete()
    except Exception as exc:
        logger.warning("Could not take a section line off the drawing (%s)", exc)


def current_reference(app, drawing_path: str, part_file: str) -> Optional[str]:
    """The path the drawing at `drawing_path` has stored for its part,
    which is what ReplaceReferencedDocument has to be given -- the checkout
    the template was made in is not the one it is used in. Asked of
    SolidWorks rather than read out of the sidecar for that reason."""
    try:
        dependencies = app.GetDocumentDependencies2(drawing_path, False, True, False)
    except Exception:
        return None
    for entry in (dependencies or []):
        if str(entry).lower().endswith(part_file.lower()) and os.sep in str(entry):
            return str(entry)
    return None


def fill_template(app, part: str, sldprt_copy: str, out_drawing: str, out_pdf: str, title: str,
                  values: Dict[str, float], material: str, layout: List[sd.View],
                  log: Callable[[str], None] = logger.info, preview: str = "") -> Optional[List[str]]:
    """Make this combination's drawing from the template.

    `layout` is `box_layout` / `lid_layout` for this combination (the caller
    has measured what it needs from the part first). Returns the list of
    things left off the sheet, or None when the template cannot be used --
    no template, or `usable` says no -- and nothing has been written; the
    caller builds from scratch then.
    """
    sidecar = load_sidecar(part)
    if sidecar is None or not os.path.isfile(template_path(part)):
        return None
    ok, why = usable(layout, sidecar)
    if not ok:
        log(f"{part}: not using the drawing template ({why})")
        return None

    shutil.copyfile(template_path(part), out_drawing)
    old_reference = current_reference(app, out_drawing, os.path.basename(str(paths.SOLIDWORKS_PART_FILES[part])))
    if not old_reference or not app.ReplaceReferencedDocument(out_drawing, old_reference, sldprt_copy):
        os.remove(out_drawing)
        log(f"{part}: not using the drawing template (could not point it at {os.path.basename(sldprt_copy)})")
        return None
    import pythoncom
    import win32com.client

    errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    drawing = app.OpenDoc6(out_drawing, 3, 1, "", errors, warnings)      # swDocDRAWING
    if drawing is None:
        os.remove(out_drawing)
        log(f"{part}: not using the drawing template (SolidWorks error {errors.value} opening it)")
        return None

    omitted: List[str] = []
    try:
        plan = classify_dims(layout, sidecar)
        views = _views_by_name(drawing)
        sheet = sd._Sheet(app, drawing, sldprt_copy, log)

        # views this combination has no use for go first, children before parents
        for key in reversed(plan["views_gone"]):
            recorded = sidecar["views"][key]
            name = recorded.get("name")
            view = views.pop(name, None)
            if view is not None:
                drawing.ClearSelection2(True)
                drawing.Extension.SelectByID2(name, "DRAWINGVIEW", 0, 0, 0, False, 0, sheet._none, 0)
                drawing.EditDelete()
                omitted.append(f"{key} view (not in this combination)")
                if recorded.get("section_of"):
                    _delete_cut_line(drawing, views, sidecar, recorded, sheet._none)

        # refit what is left to the sheet: where the generator would now choose a different
        # scale (a bigger or smaller board), scale the view by the same ratio about where it
        # stands. Positions, and any scale a person set by hand, are left as the template has
        # them -- a hand-tuned template is the point.
        for view in layout:
            recorded = sidecar["views"][view.key]
            sw = views.get(recorded["name"])
            if sw is None or not recorded.get("scale"):
                continue
            ratio = view.scale / float(recorded["scale"])
            if abs(ratio - 1.0) > 1e-6:
                where = sw.Position
                sw.UseSheetScale = False
                sw.ScaleDecimal = float(sw.ScaleDecimal) * ratio
                sw.Position = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(where))
        drawing.ForceRebuild3(False)
        views = _views_by_name(drawing)
        shown = _dimensions_by_name(views)

        # check every dimension the generator placed; keep the right ones
        for key, expected in plan["check"].items():
            name = short_name(sidecar["dims"][key]["name"])
            display = shown.get(name)
            if display is None:
                omitted.append(describe(key, "gone from the drawing"))
                continue
            try:
                dangling = bool(display.GetAnnotation.IsDangling)       # a property, not a method
            except Exception:
                dangling = False
            value = None if dangling else float(display.GetDimension2(0).Value)
            if dangling or not is_right(value, expected):
                _hide(drawing, display, sheet._none)
                omitted.append(describe(key, "dangling" if dangling else f"read {value:.3f}, expected {expected:g}"))
        for key in plan["absent"]:
            display = shown.get(short_name(sidecar["dims"][key]["name"]))
            if display is not None:
                _hide(drawing, display, sheet._none)
            omitted.append(describe(key, "not in this combination"))

        scales = ", ".join(f"{sc:g}:1" for sc in sorted({v.scale for v in layout}))
        sheet.set_properties(sd.title_properties(title, material, scales, omitted_lines(omitted)))
        drawing.ForceRebuild3(False)
        try:
            save_errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            save_warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            drawing.Save3(1, save_errors, save_warnings)                         # keep the filled drawing, in place
        except Exception as exc:
            log(f"WARNING: could not save {out_drawing}: {exc}")
        if preview:
            sheet.export_png(preview)
        sheet.save_pdf(out_pdf)
        if not os.path.isfile(out_pdf) or os.path.getsize(out_pdf) < 1000:
            raise RuntimeError("SolidWorks wrote no PDF")
        sd.check_pdf(out_pdf)
    finally:
        try:
            app.CloseDoc(drawing.GetTitle)
        except Exception:
            pass
    return omitted


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m microwave_package.design.solidworks_template [box] [lid_inside]`:
    (re)make the templates from the generator. Overwrites any a person has
    tuned, so it asks for the part by name and does nothing by default."""
    import sys

    parts = [a for a in (argv if argv is not None else sys.argv[1:]) if a in TEMPLATE_FILES]
    if not parts:
        print("name the template(s) to rebuild:", ", ".join(TEMPLATE_FILES), "(this overwrites a hand-tuned one)")
        return 2
    import pythoncom

    pythoncom.CoInitialize()
    app, err = step_generation.connect_solidworks()
    if not app:
        print("could not connect to SolidWorks:", err)
        return 1
    for part in parts:
        left_off = build_template(app, part, print)
        if left_off:
            print(part, "left off:", "; ".join(left_off))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
