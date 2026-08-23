"""
parameters_svg.py

Connects to SolidWorks, opens a .SLDDRW file, and exports EACH drawing view on
the sheet as its own separate file - SVG by default, since these views are meant
for a web UI (vector, scales at any zoom, themeable via CSS, embeds inline).

How it works, and why it works this way:

  * SolidWorks cannot export SVG. Its Save As only offers DWG/DXF/PDF/images for
    drawings (asking for a .svg returns error 256). So each view is exported to
    DXF and converted to SVG locally with ezdxf.

  * There is no API call that exports a single view by itself. The usual macro
    trick is to suppress every other view first - but view suppression silently
    does nothing on this drawing's views (SetSuppression2 returns True and
    changes nothing; EditSuppress2 returns False). Deleting the other views
    does work, so that is what we do.

  * Because we delete views, we never touch the real file: the drawing is copied
    to a temporary .SLDDRW next to it (same folder, so its references to the
    part/assembly files still resolve), and every open/delete/export happens on
    that copy, which is closed without saving and removed at the end.

  * Views can overlap on the sheet, so "export the whole sheet once and split it
    up by view bounding box" does not work - roughly half the entities fall
    inside two or more view outlines. Hence one isolated export per view.

  * Every export still carries the sheet format (A0 border, zone letters, title
    block) mixed in with the view on layer '0', and the sheet format cannot be
    deleted through the API. Since it is byte-identical in every export, it is
    exported once on its own - with all views deleted - and those exact entities
    are then subtracted from each view's export.

Requirements:
    - Windows, with SolidWorks installed and licensed.
    - pywin32   (pip install pywin32)
    - ezdxf     (pip install ezdxf)      [only needed for --format svg]

Usage:
    python parameters_svg.py "all_components.SLDDRW"
    python parameters_svg.py all_components.SLDDRW --format pdf
    python parameters_svg.py all_components.SLDDRW --views chip_width port_count
"""

import argparse
import collections
import os
import shutil
import sys
import tempfile
import time
import winreg

try:
    import win32com.client
    import win32com.client.makepy as makepy
    import pythoncom
    from win32com.client import gencache
except ImportError:
    sys.exit(
        "This script needs pywin32. Install it with:\n"
        "    pip install pywin32\n"
        "and run this script on Windows with SolidWorks installed."
    )

# ---------------------------------------------------------------------------
# SolidWorks API constants (from the swconst type library).
# ---------------------------------------------------------------------------
SW_PROGID = "SldWorks.Application"

swDocDRAWING = 3
swOpenDocOptions_Silent = 1

swSaveAsCurrentVersion = 0
swSaveAsOptions_Silent = 1

# swInConfigurationOpts_e
swThisConfiguration = 2

# Formats SolidWorks can write directly; svg is produced by converting dxf.
NATIVE_FORMATS = ("dxf", "dwg", "pdf")
FORMATS = ("svg",) + NATIVE_FORMATS


def log(msg):
    print(f"[parameters_svg] {msg}")


# ---------------------------------------------------------------------------
# COM plumbing
# ---------------------------------------------------------------------------
def _registered_exe_path(progid=SW_PROGID):
    """Path of the SLDWORKS.exe that is registered for this ProgID."""
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID") as key:
        clsid = winreg.QueryValue(key, None)
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32") as key:
        return winreg.QueryValue(key, None)


def load_solidworks_module():
    """Return the makepy wrapper module for the installed SolidWorks typelib.

    SolidWorks' typelib is registered under stale version keys on machines with
    parallel installs (e.g. registered as "20.0" while the exe reports 32.0), so
    the object cannot describe itself over COM: GetTypeInfo fails with 'Element
    not found', which makes EnsureDispatch and CastTo fail with 'Member not
    found' / 'This COM object can not automate the makepy process'.

    Loading sldworks.tlb straight off disk sidesteps the registry entirely.
    """
    tlb_path = os.path.join(os.path.dirname(_registered_exe_path()), "sldworks.tlb")
    if not os.path.isfile(tlb_path):
        raise RuntimeError(f"SolidWorks type library not found: {tlb_path}")

    tlb = pythoncom.LoadTypeLib(tlb_path)
    attr = tlb.GetLibAttr()
    guid, lcid, major, minor = str(attr[0]), attr[1], attr[3], attr[4]

    try:
        mod = gencache.GetModuleForTypelib(guid, lcid, major, minor)
    except Exception:
        mod = None
    if mod is None:
        log("Generating SolidWorks COM wrappers (one-time, takes a minute)...")
        makepy.GenerateFromTypeLibSpec(tlb)
        mod = gencache.GetModuleForTypelib(guid, lcid, major, minor)
    if mod is None:
        raise RuntimeError("Could not build the SolidWorks COM wrapper module.")
    return mod


def solidworks_is_running():
    try:
        pythoncom.GetActiveObject(SW_PROGID)
        return True
    except Exception:
        return False


def connect_to_solidworks(visible):
    log("Connecting to SolidWorks (launching it if it isn't already running)...")
    sw_app = win32com.client.Dispatch(SW_PROGID)
    sw_app.Visible = visible
    return sw_app


class Api:
    """Binds raw COM pointers to their real interfaces.

    Every object SolidWorks hands back arrives as a bare IDispatch that only
    resolves a handful of legacy members by name (GetTitle works, Extension and
    FirstFeature do not). Wrapping the pointer in the generated class makes
    calls go out by numeric DISPID instead, which works regardless.
    """

    def __init__(self, mod):
        self.ModelDoc2 = mod.IModelDoc2
        self.DrawingDoc = mod.IDrawingDoc
        self.View = mod.IView

    @staticmethod
    def cast(klass, obj):
        if obj is None:
            return None
        return klass(getattr(obj, "_oleobj_", obj))


def open_drawing(sw_app, api, path):
    errors = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    raw = sw_app.OpenDoc6(path, swDocDRAWING, swOpenDocOptions_Silent, "", errors, warnings)
    if raw is None:
        raise RuntimeError(
            f"SolidWorks failed to open '{path}' "
            f"(error code {errors.value}, warning code {warnings.value})."
        )
    return api.cast(api.ModelDoc2, raw), api.cast(api.DrawingDoc, raw)


def list_views(api, drawing):
    """(name, outline) for every view on the active sheet.

    The first entry in the chain is the sheet itself, not a view, so it is
    skipped. Outlines are (x1, y1, x2, y2) in metres, in sheet coordinates.
    """
    views = []
    view = api.cast(api.View, drawing.GetFirstView())
    is_sheet = True
    while view is not None:
        if not is_sheet:
            views.append((view.GetName2(), view.GetOutline()))
        is_sheet = False
        view = api.cast(api.View, view.GetNextView())
    return views


def save_as(model_ext, out_path):
    """SaveAs3 reports [out] params through the return value: (ok, err, warn)."""
    result = model_ext.SaveAs3(
        out_path,
        swSaveAsCurrentVersion,
        swSaveAsOptions_Silent,
        None,
        None,
        0,
        0,
    )
    if isinstance(result, tuple):
        ok, errors, warnings = result[0], result[1], result[2]
    else:
        ok, errors, warnings = result, 0, 0
    return bool(ok), errors, warnings


# ---------------------------------------------------------------------------
# DXF -> SVG
# ---------------------------------------------------------------------------
def entity_signature(entity):
    """A stable identity for one DXF entity, used to match the sheet format.

    The sheet format comes out identical in every export, so type + position
    (+ text, since zone letters differ only by their content) is enough to
    recognise it again.
    """
    import ezdxf.bbox

    try:
        bounds = ezdxf.bbox.extents([entity], fast=True)
    except Exception:
        bounds = None
    if bounds is not None and bounds.has_data:
        where = (
            round(bounds.extmin.x, 3), round(bounds.extmin.y, 3),
            round(bounds.extmax.x, 3), round(bounds.extmax.y, 3),
        )
    else:
        where = None

    kind = entity.dxftype()
    text = ""
    try:
        if kind == "MTEXT":
            text = entity.text
        elif kind in ("TEXT", "ATTRIB"):
            text = entity.dxf.text
    except Exception:
        pass
    return kind, where, text


def signature_counter(dxf_path):
    import ezdxf

    doc = ezdxf.readfile(dxf_path)
    return collections.Counter(entity_signature(e) for e in doc.modelspace())


def add_missing_dimension_text(doc, msp):
    """Put the measured value back onto each dimension.

    SolidWorks exports a dimension's line and arrowheads but leaves the number
    out of its geometry block (it writes text '<>', "use the measured value",
    and then never writes that value), so dimensions come out of the DXF with
    arrows and no figure. The number is rebuilt here from the measurement, the
    style's length factor (dimlfac - 0.2 on a 5:1 sheet) and its precision.
    """
    added = 0
    for dim in msp.query("DIMENSION"):
        already_drawn = False
        try:
            for part in dim.virtual_entities():
                if part.dxftype() in ("MTEXT", "TEXT"):
                    already_drawn = True
                    break
        except Exception:
            pass
        if already_drawn:
            continue

        location = dim.dxf.get("text_midpoint", None)
        if location is None:
            continue
        try:
            measurement = float(dim.get_measurement())
        except Exception:
            continue

        factor, decimals, height = 1.0, 2, 2.5
        try:
            style = doc.dimstyles.get(dim.dxf.get("dimstyle", "Standard"))
            factor = float(style.dxf.get("dimlfac", 1.0)) or 1.0
            decimals = int(style.dxf.get("dimdec", 2))
            height = float(style.dxf.get("dimtxt", 2.5)) or 2.5
        except Exception:
            pass

        label = f"{measurement * factor:.{decimals}f}"
        override = (dim.dxf.get("text", "") or "").strip()
        if override and override != "<>":
            label = override.replace("<>", label)

        msp.add_mtext(label, dxfattribs={
            "insert": location,
            "char_height": height,
            "attachment_point": 5,  # middle-centre, like a dimension figure
            "layer": dim.dxf.get("layer", "0"),
        })
        added += 1
    return added


def dxf_to_svg(dxf_path, svg_path, format_signatures, dimension_text=True, margin_mm=5.0):
    """Strip the sheet format out of a one-view export and render it as SVG."""
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, layout
    from ezdxf.addons.drawing import svg as svg_backend
    from ezdxf.addons.drawing.config import (
        BackgroundPolicy,
        ColorPolicy,
        Configuration,
    )

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    budget = collections.Counter(format_signatures)
    kept = 0
    for entity in list(msp):
        signature = entity_signature(entity)
        if budget[signature] > 0:
            budget[signature] -= 1
            msp.delete_entity(entity)
        else:
            kept += 1

    if dimension_text:
        add_missing_dimension_text(doc, msp)

    config = Configuration(
        color_policy=ColorPolicy.CUSTOM,
        custom_fg_color="#000000",
        background_policy=BackgroundPolicy.OFF,
    )
    backend = svg_backend.SVGBackend()
    Frontend(RenderContext(doc), backend, config=config).draw_layout(msp)
    page = layout.Page(0, 0, layout.Units.mm, layout.Margins.all(margin_mm))
    svg = backend.get_string(page)

    # let the page's CSS colour drive the strokes, so dark mode works
    svg = svg.replace("#000000", "currentColor")

    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(svg)
    return kept


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def sanitize_filename(name):
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return name.strip() or "view"


def isolate_views(model, drawing, api, keep):
    """Delete every view except the ones named in `keep`. Returns what is left."""
    extension = model.Extension
    for name in [n for n, _ in list_views(api, drawing)]:
        if name in keep:
            continue
        model.ClearSelection2(True)
        if extension.SelectByID2(name, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0):
            # EditDelete returns None even when it succeeds, so the result is
            # verified by re-listing the views rather than by its return value.
            model.EditDelete()
    model.ClearSelection2(True)
    model.EditRebuild3()
    return [name for name, _ in list_views(api, drawing)]


def export_sheet_format(sw_app, api, working_copy, out_path):
    """Export the sheet format on its own, with every view deleted."""
    model, drawing = open_drawing(sw_app, api, working_copy)
    try:
        remaining = isolate_views(model, drawing, api, keep=())
        if remaining:
            log(f"  WARNING: {len(remaining)} view(s) survived the sheet-format pass")
        ok, errors, warnings = save_as(model.Extension, out_path)
        if not ok or not os.path.exists(out_path):
            raise RuntimeError(
                f"Could not export the sheet format (error {errors}, warning {warnings})."
            )
        return signature_counter(out_path)
    finally:
        try:
            sw_app.CloseDoc(model.GetTitle())
        except Exception:
            pass


def export_view(sw_app, api, working_copy, target, out_dir, fmt, format_signatures,
                keep_dxf, dimension_text=True):
    """Isolate one view in the working copy and export it."""
    model, drawing = open_drawing(sw_app, api, working_copy)
    try:
        remaining = isolate_views(model, drawing, api, keep={target})
        if remaining != [target]:
            log(f"  WARNING: expected only '{target}' to remain, found {remaining}")

        stem = os.path.join(out_dir, sanitize_filename(target))
        native_ext = "dxf" if fmt == "svg" else fmt
        native_path = f"{stem}.{native_ext}"

        ok, errors, warnings = save_as(model.Extension, native_path)
        if not ok or not os.path.exists(native_path):
            log(f"  FAILED to export '{target}' (error {errors}, warning {warnings})")
            return None

        if fmt != "svg":
            return native_path

        svg_path = f"{stem}.svg"
        kept = dxf_to_svg(native_path, svg_path, format_signatures, dimension_text)
        if not keep_dxf:
            os.remove(native_path)
        log(f"  saved: {svg_path}  ({kept} entities)")
        return svg_path
    finally:
        try:
            sw_app.CloseDoc(model.GetTitle())
        except Exception:
            pass


def remove_with_retry(path, attempts=6):
    for _ in range(attempts):
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except PermissionError:
            time.sleep(1)
    return not os.path.exists(path)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("slddrw_path", help="Path to the .SLDDRW file to convert")
    parser.add_argument("--output-dir", default=None,
                        help="Folder to write into (default: <input folder>\\<filename>_drawings)")
    parser.add_argument("--format", default="svg", choices=FORMATS,
                        help="Output format (default: svg)")
    parser.add_argument("--views", nargs="+", default=None,
                        help="Only export these view names (default: all)")
    parser.add_argument("--list", action="store_true",
                        help="List the drawing's views and exit")
    parser.add_argument("--keep-dxf", action="store_true",
                        help="Keep the intermediate .dxf files alongside the SVGs")
    parser.add_argument("--no-dimension-text", action="store_true",
                        help="Leave dimensions as arrows only, without the measured "
                             "value (useful if the web UI prints the value itself)")
    parser.add_argument("--visible", action="store_true",
                        help="Show the SolidWorks UI while running")
    parser.add_argument("--keep-open", action="store_true",
                        help="Leave SolidWorks running even if this script started it")
    args = parser.parse_args()

    input_path = os.path.abspath(args.slddrw_path)
    if not os.path.isfile(input_path):
        sys.exit(f"File not found: {input_path}")

    if args.format == "svg":
        try:
            import ezdxf  # noqa: F401
        except ImportError:
            sys.exit(
                "SVG output needs ezdxf (SolidWorks cannot write SVG itself).\n"
                "    pip install ezdxf\n"
                "or pick a format SolidWorks writes directly: --format dxf|dwg|pdf"
            )

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_dir = args.output_dir or os.path.join(
        os.path.dirname(input_path), f"{base_name}_drawings"
    )

    mod = load_solidworks_module()
    api = Api(mod)

    was_running = solidworks_is_running()
    sw_app = connect_to_solidworks(visible=args.visible)

    # Work on a copy in the same folder, so the drawing's references to the
    # part/assembly files still resolve and the real file is never modified.
    working_copy = os.path.join(
        os.path.dirname(input_path), f"~{base_name}_swexport_{os.getpid()}.SLDDRW"
    )
    log(f"Copying to a temporary working file: {os.path.basename(working_copy)}")
    shutil.copy2(input_path, working_copy)
    scratch_dir = tempfile.mkdtemp(prefix="parameters_svg_")

    start = time.time()
    exported = []
    try:
        model, drawing = open_drawing(sw_app, api, working_copy)
        views = list_views(api, drawing)
        sw_app.CloseDoc(model.GetTitle())

        if not views:
            log("No drawing views found on the active sheet - nothing to export.")
            return

        names = [name for name, _ in views]
        log(f"Found {len(names)} drawing view(s): {', '.join(names)}")

        if args.list:
            return

        targets = names
        if args.views:
            unknown = [name for name in args.views if name not in set(names)]
            if unknown:
                sys.exit(f"No such view(s): {', '.join(unknown)}")
            targets = [name for name in names if name in set(args.views)]

        format_signatures = collections.Counter()
        if args.format == "svg":
            log("Exporting the sheet format on its own, to subtract it later...")
            format_signatures = export_sheet_format(
                sw_app, api, working_copy, os.path.join(scratch_dir, "sheet_format.dxf")
            )
            log(f"  sheet format: {sum(format_signatures.values())} entities")

        os.makedirs(output_dir, exist_ok=True)
        for index, target in enumerate(targets, 1):
            log(f"[{index}/{len(targets)}] {target}")
            path = export_view(
                sw_app, api, working_copy, target,
                output_dir, args.format, format_signatures, args.keep_dxf,
                dimension_text=not args.no_dimension_text,
            )
            if path:
                exported.append(path)

        log(f"Done. Exported {len(exported)}/{len(targets)} file(s) to: {output_dir}")
        log(f"Elapsed: {time.time() - start:.1f}s")
    finally:
        if not was_running and not args.keep_open:
            try:
                sw_app.ExitApp()
            except Exception:
                pass
            time.sleep(1)
        if not remove_with_retry(working_copy):
            log(f"NOTE: could not remove the temporary file: {working_copy}")
        shutil.rmtree(scratch_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
