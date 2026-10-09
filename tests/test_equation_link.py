"""A SolidWorks part reads its dimensions from an equations file it is linked
to by absolute path, saved in the part. Parts saved in another checkout read
THAT checkout's file, so a run's own values (no screws, a different pedestal)
never reached the box and lid. The pipeline re-points the link.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.design import step_generation as sg


class FakeManager:
    def __init__(self, path):
        self.FilePath = path
        self.updated = 0

    @property
    def UpdateValuesFromExternalEquationFile(self):     # a property under late-bound COM
        self.updated += 1
        return True


class FakePart:
    def __init__(self, path, broken=False):
        self.mgr = FakeManager(path)
        self.rebuilds = 0
        self.broken = broken

    @property
    def GetEquationMgr(self):
        if self.broken:
            raise RuntimeError("no equation manager")
        return self.mgr

    def ForceRebuild3(self, top_only):
        self.rebuilds += 1


MINE = r"C:\work\main\base_parts\default.txt"
THEIRS = r"C:\work\worktrees\m2-corner-screws\base_parts\default.txt"


class TestLinkEquations(unittest.TestCase):
    def test_a_part_linked_elsewhere_is_pointed_at_this_file_and_reread(self):
        part, said = FakePart(THEIRS), []
        self.assertTrue(sg.link_equations(part, MINE, log=said.append))
        self.assertEqual(part.mgr.FilePath, MINE)
        self.assertEqual((part.mgr.updated, part.rebuilds), (1, 1))
        self.assertIn(THEIRS, said[0])
        self.assertIn(MINE, said[0])

    def test_a_part_already_linked_here_is_left_alone(self):
        part = FakePart(MINE)
        self.assertFalse(sg.link_equations(part, MINE))
        self.assertEqual((part.mgr.updated, part.rebuilds), (0, 0))

    def test_paths_are_compared_the_way_windows_does(self):
        part = FakePart(MINE.upper())
        self.assertFalse(sg.link_equations(part, MINE.lower().replace("\\", "/")))

    def test_a_part_with_no_link_is_left_alone(self):
        part = FakePart("")
        self.assertFalse(sg.link_equations(part, MINE))
        self.assertEqual(part.mgr.FilePath, "")

    def test_an_equation_manager_that_cannot_be_read_is_reported_not_fatal(self):
        said = []
        self.assertFalse(sg.link_equations(FakePart(THEIRS, broken=True), MINE, log=said.append))
        self.assertTrue(any("WARNING" in line for line in said))


if __name__ == "__main__":
    unittest.main()
