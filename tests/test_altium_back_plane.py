"""The bottom copper under the die's opening, in the Altium generator.

The rule (project owner, 2026-11-10): when there is a chip on the PCB there
is a matching hole in the bottom copper. Through or blind, the opening has
its copper cut away underneath; a blind pocket keeps only its laminate
floor. A bare board, with no opening, keeps its whole bottom plane. The die
has no plane at all.

DelphiScript cannot run here, so these read the source the way the other
Altium tests do: { } comments are stripped, then the calls and the
rectangles that decide the plane are checked for shape.

Run: python -m unittest discover -s tests
"""
import re
import unittest

from microwave_package import paths


def _code():
    """The script without its { ... } comments. Pascal braces do not nest,
    and no string in the script holds one, so a plain match is exact."""
    text = paths.ALTIUM_GENERATOR.read_text(encoding="utf-8", errors="replace")
    return re.sub(r"\{[^}]*\}", " ", text)


def _squeezed(text):
    """Whitespace removed, so the layout of the source does not matter."""
    return re.sub(r"\s+", "", text)


def _procedure(code, header):
    """The text from `header` to the End; that closes it at column 0."""
    start = code.index(header)
    return code[start:code.index("\nEnd;", start)]


def _close(code, open_at):
    """Index of the ')' that closes the '(' at open_at."""
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


def _items(text, sep):
    """The items of `text` split on `sep`, ignoring separators in brackets."""
    items, depth, current = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += ch
    items.append(current.strip())
    return items


def _call_args(code, match):
    """The arguments of the call whose '(' is the last character `match` took."""
    open_at = match.end() - 1
    return _items(code[open_at + 1:_close(code, open_at)], ",")


class TestTheFarFaceIsCutUnderEveryOpening(unittest.TestCase):
    """Through or blind, the plane on the far face goes round the opening."""

    @classmethod
    def setUpClass(cls):
        cls.code = _code()
        cls.build_board = _procedure(cls.code, "Procedure BuildBoard(")
        cls.back_plane = _procedure(cls.code, "Procedure AddBackPlane(")

    def test_the_back_plane_is_not_decided_by_through_or_blind(self):
        self.assertIn("IfWantBackPlaneThenAddBackPlane(Board,CopperHalfL,CopperHalfS,CavHalfL,CavHalfS);",
                      _squeezed(self.build_board))
        self.assertNotIn("CavThrough", self.build_board)

    def test_there_is_one_call_to_the_plane(self):
        calls = re.findall(r"(?<!Procedure )AddBackPlane\(", self.code)
        self.assertEqual(len(calls), 1, "a second call would be a second branch deciding the plane")

    def test_a_bare_board_keeps_one_whole_rectangle(self):
        self.assertIn(
            "IfCavHalfL<=0.0ThenBegin"
            "AddRect(Board,-CopperHalfL,-CopperHalfS,CopperHalfL,CopperHalfS,eBottomLayer);"
            "Exit;End;",
            _squeezed(self.back_plane))

    def test_an_opening_is_four_pieces_round_the_hole_and_nothing_more(self):
        squeezed = _squeezed(self.back_plane)
        pieces = (
            "AddRect(Board,-CopperHalfL,-CopperHalfS,-CavHalfL,CopperHalfS,eBottomLayer);",
            "AddRect(Board,CavHalfL,-CopperHalfS,CopperHalfL,CopperHalfS,eBottomLayer);",
            "AddRect(Board,-StripHalfL,-CopperHalfS,StripHalfL,-CavHalfS,eBottomLayer);",
            "AddRect(Board,-StripHalfL,CavHalfS,StripHalfL,CopperHalfS,eBottomLayer);",
        )
        for piece in pieces:
            with self.subTest(piece=piece):
                self.assertEqual(squeezed.count(piece), 1)
        self.assertEqual(squeezed.count("AddRect(Board,"), 5,
                         "the whole plane for a bare board, and the four pieces")
        self.assertEqual(squeezed.count("eBottomLayer"), 5)

    def test_a_bare_board_has_no_opening_so_its_plane_is_whole(self):
        self.assertIn("IfNoChipThenBeginCavHalfL:=0.0;CavHalfS:=0.0;", _squeezed(self.code))


class TestEveryBuildBoardCallMatchesItsDefinition(unittest.TestCase):
    """A call with the wrong number of arguments is a compile error in
    DelphiScript, shown as a dialog that no Try can catch, and nothing here
    can run the script to find it. So the arguments are counted."""

    @classmethod
    def setUpClass(cls):
        cls.code = _code()
        head = re.search(r"Procedure\s+BuildBoard\(", cls.code)
        groups = _items(cls.code[head.end():_close(cls.code, head.end() - 1)], ";")
        cls.declared = sum(len(group.split(":")[0].split(",")) for group in groups)
        cls.calls = [_call_args(cls.code, m)
                     for m in re.finditer(r"(?<!Procedure )BuildBoard\(", cls.code)]

    def test_the_definition_declares_sixteen_parameters(self):
        self.assertEqual(self.declared, 16)

    def test_there_are_two_calls_and_each_passes_every_parameter(self):
        self.assertEqual(len(self.calls), 2, "the package and the die")
        for args in self.calls:
            with self.subTest(args=args):
                self.assertEqual(len(args), self.declared)

    def test_the_package_passes_the_opening_and_a_plane(self):
        package = next(args for args in self.calls if args[1] == "HalfL")
        self.assertEqual(package[13:], ["CavHalfL", "CavHalfS", "True"])

    def test_the_die_passes_no_opening_and_no_plane(self):
        die = next(args for args in self.calls if args[1] == "ChipHalfL")
        self.assertEqual(die[13:], ["0.0", "0.0", "False"])


class TestNoViaLandsInTheHole(unittest.TestCase):
    """A blind pocket's hole is as much a hole as a through cut's, so the
    via keep-out must not depend on which kind the opening is."""

    def test_the_via_keep_out_is_the_same_for_any_opening(self):
        tryvia = _squeezed(_procedure(_code(), "Function TryVia("))
        self.assertIn(
            "If((CavHalfL>0.0)Or(CavHalfS>0.0))"
            "And(Abs(X)<CavHalfL+Dia/2.0+MinGap-GapTolerance)"
            "And(Abs(Y)<CavHalfS+Dia/2.0+MinGap-GapTolerance)ThenExit;",
            tryvia)


if __name__ == "__main__":
    unittest.main()
