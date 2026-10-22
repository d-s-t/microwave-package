"""
Removes the distortion (the blind pockets in the box under the holes in the
PCB, see VENTING.md item 10) from base_parts/box.SLDPRT, together with the
equations that drive it, so the option is gone from the project.

Run once on the machine with SolidWorks, with nothing else using it:

    python base_parts/remove_distortion.py                    # dry run: list what would go
    python base_parts/remove_distortion.py --apply            # delete, then save
    python base_parts/remove_distortion.py --apply --dump F   # also write the equation lists before/after to F

Running it again on a part with no distortion left does nothing.

A distortion feature is any feature whose name contains "distort", or whose
own equations (its suppression, its dimensions) refer to no_distortion,
distortion_radius, distortion_x or distortion_y. A sketch is removed with it
only when every feature hanging off that sketch is itself a distortion
feature. The script refuses to act (and changes nothing) when:

  - a distortion feature has a child that is not also a distortion feature
    (for example a vent or a fillet built on it), or
  - the children of a feature cannot be read, or
  - an equation outside the distortion refers to no_distortion,
    distortion_radius, distortion_x or distortion_y, or to a feature being
    removed, or
  - the equations left after the change are not exactly the ones before
    minus the removed ones.

The equations go first, by their text, because deleting a feature shifts the
row numbers of the rest. The features then go one at a time, last in the
tree first, each selected by name, so no multi-select delete dialog can
block the run. The part is saved only after every check has passed; any
refusal closes the part without saving it.

The part's equation link is pointed at THIS checkout's default.txt first, as
add_vents.py does, and the old link is printed.
"""
import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from microwave_package.design import step_generation as sg  # noqa: E402

INPUT_DIM_ON_CREATE = 10
DISTORT = re.compile(r"distort", re.IGNORECASE)
RETIRED = re.compile(r"\b(no_distortion|distortion_radius|distortion_x|distortion_y)\b")
RETIRED_NAMES = ("no_distortion", "distortion_radius", "distortion_x", "distortion_y")
FULLWIDTH_AT = "＠"  # SolidWorks writes this for "@" in some equations after an in-memory edit


def ascii_form(text):
    """An equation in one form, whichever way SolidWorks wrote it: a plain "@" (not the
    full-width one), no part-file suffix on a feature's name ("D1@Sketch6@box.Part" becomes
    "D1@Sketch6"), and no comment after the apostrophe."""
    t = text.replace(FULLWIDTH_AT, "@").split("'", 1)[0]
    return re.sub(r'("[^"@]*@[^"@]*)@[^"]*"', r'\1"', t)


def normal_equation(text):
    """ascii_form with the whitespace removed: what two readings of one equation share."""
    return re.sub(r"\s+", "", ascii_form(text))


def owner_of(text):
    """The feature an equation belongs to ("D2@Sketch3" gives Sketch3), or the global name
    itself ("no_distortion" gives no_distortion)."""
    lhs = ascii_form(text).partition("=")[0].strip().strip('"')
    return lhs.split("@", 1)[1].strip('"') if "@" in lhs else lhs


def features(m):
    f = m.FirstFeature
    while f:
        yield f
        f = f.GetNextFeature


def find_feature(m, name):
    for f in features(m):
        if f.Name == name:
            return f
    return None


def equations(m):
    em = m.GetEquationMgr
    return [em.Equation(i) for i in range(em.GetCount)]


def children_of(feature):
    try:
        kids = feature.GetChildren
        return list(kids) if kids else []
    except Exception as exc:
        sys.exit(f"REFUSED: could not read the children of {feature.Name} ({exc}). Nothing was changed.")


def plan(m):
    """(feature names to delete, last in the tree first; equation texts to delete).
    Exits to refuse."""
    feats = list(features(m))
    feat_names = {f.Name for f in feats}
    targets = {f.Name for f in feats if DISTORT.search(f.Name)}
    # A feature or sketch that is driven by a retired parameter (its suppression, its dimensions)
    # is part of the distortion, whatever it is called: box.SLDPRT's distortion boss is named
    # Boss-Extrude7 and is suppressed by no_distortion.
    for text in equations(m):
        owner = owner_of(text)
        if RETIRED.search(ascii_form(text)) and owner in feat_names:
            targets.add(owner)
    # A sketch goes with the distortion only if everything built on it is a distortion feature.
    for f in feats:
        if f.Name in targets or not f.Name.lower().startswith("sketch"):
            continue
        kids = [c.Name for c in children_of(f)]
        if kids and all(k in targets for k in kids):
            targets.add(f.Name)
    for f in feats:
        if f.Name not in targets:
            continue
        for c in children_of(f):
            if c.Name not in targets:
                sys.exit(f"REFUSED: {f.Name} has a child {c.Name} that is not a distortion feature. "
                         "Nothing was changed.")
    doomed, kept = [], []
    for text in equations(m):
        owner = owner_of(text)
        if owner in targets or owner in RETIRED_NAMES:
            doomed.append(text)
        else:
            kept.append(text)
    for text in kept:
        if RETIRED.search(ascii_form(text)):
            sys.exit(f"REFUSED: equation {text!r} outside the distortion refers to a retired parameter. "
                     "Nothing was changed.")
        for name in targets:
            if re.search(re.escape("@" + name) + r"(?![\w-])", ascii_form(text)):
                sys.exit(f"REFUSED: equation {text!r} outside the distortion refers to {name}, which "
                         "is being removed. Nothing was changed.")
    order = [f.Name for f in feats if f.Name in targets]
    return order, doomed


def delete_features(m, names):
    """Delete one feature at a time, last in the tree first, so a cut goes before its sketch."""
    for name in reversed(names):
        m.ClearSelection2(True)
        f = find_feature(m, name)
        if f is None:
            continue  # already gone with an earlier delete
        f.Select2(False, 0)
        m.EditDelete()
    m.ClearSelection2(True)


def write_dump(path, label, eqs):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"== {label} ({len(eqs)} equations)\n")
        for e in eqs:
            fh.write(e + "\n")


def main():
    import pythoncom
    import win32com.client

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="delete and save (default: dry run)")
    ap.add_argument("--dump", help="write the equation lists before and after to this file")
    args = ap.parse_args()
    root = os.path.dirname(os.path.abspath(__file__))
    part = os.path.join(root, "box.SLDPRT")
    equations_path = os.path.join(root, "default.txt")

    app, err = sg.connect_solidworks()
    if not app:
        sys.exit(err)
    was = app.GetUserPreferenceToggle(INPUT_DIM_ON_CREATE)
    app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, False)
    m = None
    try:
        m, err = sg.open_and_activate_part(app, part)
        if not m:
            sys.exit(err)
        em = m.GetEquationMgr
        print("equation link before:", em.FilePath or "(none)")
        sg.link_equations(m, equations_path)

        before = equations(m)
        targets, doomed = plan(m)
        print("features to delete (tree order):", targets or "none")
        print("equations to delete:", doomed or "none")
        if args.dump:
            write_dump(args.dump, "before", before)
        if not targets and not doomed:
            print("nothing to do: the part has no distortion left")
            return
        if not args.apply:
            print("dry run: run again with --apply to delete")
            return

        for text in doomed:
            now = equations(m)
            hit = next((i for i, e in enumerate(now) if normal_equation(e) == normal_equation(text)), None)
            if hit is None:
                sys.exit(f"equation {text!r} is not in the part any more. The part was NOT saved.")
            em.Delete(hit)
        delete_features(m, targets)
        m.ForceRebuild3(False)

        left = [t for t in targets if find_feature(m, t) is not None]
        if left:
            sys.exit(f"these features were not deleted: {left}. The part was NOT saved.")
        after = equations(m)
        if args.dump:
            write_dump(args.dump, "after", after)
        # Compare in one form: SolidWorks may write the same equation with a full-width "@",
        # a part-file suffix or other spacing after the in-memory delete.
        expected = (collections.Counter(normal_equation(e) for e in before)
                    - collections.Counter(normal_equation(e) for e in doomed))
        actual = collections.Counter(normal_equation(e) for e in after)
        if actual != expected:
            extra = sorted((actual - expected).elements())
            missing = sorted((expected - actual).elements())
            sys.exit(f"the equations left are not the ones expected (compared in one form): "
                     f"extra {extra}, missing {missing}. The part was NOT saved.")
        if any(RETIRED.search(ascii_form(e)) for e in after):
            sys.exit("a retired parameter is still in the part's equations. The part was NOT saved.")
        err_v = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warn_v = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        print("save returned", m.Save3(1, err_v, warn_v), "errors", err_v.value, "warnings", warn_v.value)
        print("removed", len(targets), "feature(s) and", len(doomed), "equation(s); check the file's hash and mtime")
    finally:
        if m is not None:
            app.CloseDoc(os.path.basename(part))  # any change not saved above is discarded here
        app.SetUserPreferenceToggle(INPUT_DIM_ON_CREATE, was)


if __name__ == "__main__":
    main()
