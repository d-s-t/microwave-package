"""Tests for the check a model passes on this machine before it is queued.

The check reopens the saved project in AEDT, which these tests cannot
do; what they pin is the part that is pure logic -- which bodies count
as dielectric, which pairs are worth intersecting, and what counts as an
overlap.

Run: python -m unittest discover -s tests
"""
import unittest

from microwave_package.hfss import model_builder


class FakeBody:
    def __init__(self, name, box, solve_inside=True):
        self.name = name
        self.bounding_box = list(box)
        self.solve_inside = solve_inside
        self.faces = [1]


class FakeModeler:
    """Intersecting two copies gives a body whose volume is looked up by
    the pair of originals it came from."""

    def __init__(self, bodies, overlaps):
        self._bodies = {b.name: b for b in bodies}
        self.overlaps = overlaps
        self.intersected = []
        self.deleted = []

    @property
    def solid_objects(self):
        return [b for b in self._bodies.values() if not b.name.startswith("copy_")]

    @property
    def object_names(self):
        return list(self._bodies)

    def __getitem__(self, name):
        return self._bodies[name]

    def clone(self, name):
        copy = FakeBody("copy_" + name, self._bodies[name].bounding_box)
        copy.source = name
        self._bodies[copy.name] = copy
        return True, [copy.name]

    def intersect(self, names, keep_originals=False):
        a, b = (self._bodies[n].source for n in names)
        self.intersected.append(tuple(sorted((a, b))))
        volume = self.overlaps.get(tuple(sorted((a, b))), 0.0)
        result = self._bodies[names[0]]
        del self._bodies[names[1]]
        if not volume:
            del self._bodies[names[0]]
            return None
        result.volume = volume
        return result.name

    def delete(self, names):
        self.deleted.extend(names)
        for name in names:
            self._bodies.pop(name, None)


class FakeHfss:
    def __init__(self, bodies, overlaps=None):
        self.modeler = FakeModeler(bodies, overlaps or {})


BOARD = [-25, 1.0, -12.5, 25, 1.381, 12.5]
VACUUM = [-25, 0.0, -12.5, 25, 2.0, 12.5]


class TestDielectricOverlaps(unittest.TestCase):
    def test_two_dielectrics_sharing_volume_are_reported(self):
        hfss = FakeHfss(
            [FakeBody("pcb_substrate", BOARD), FakeBody("intruder", [10, 1.1, -1, 12, 1.3, 1])],
            {("intruder", "pcb_substrate"): 0.8},
        )
        self.assertEqual(model_builder.dielectric_overlaps(hfss),
                         [("pcb_substrate", "intruder", 0.8)])

    def test_metal_over_a_dielectric_is_the_override_working_not_an_overlap(self):
        hfss = FakeHfss([FakeBody("vacuum", VACUUM), FakeBody("box", VACUUM, solve_inside=False)],
                        {("box", "vacuum"): 100.0})
        self.assertEqual(model_builder.dielectric_overlaps(hfss), [])
        self.assertEqual(hfss.modeler.intersected, [])

    def test_bodies_that_only_touch_are_not_an_overlap(self):
        """The carve leaves vacuum and board sharing faces: an
        intersection of nothing, or of floating-point crumbs."""
        hfss = FakeHfss([FakeBody("vacuum", VACUUM), FakeBody("pcb_substrate", BOARD)],
                        {("pcb_substrate", "vacuum"): 1e-9})
        self.assertEqual(model_builder.dielectric_overlaps(hfss), [])

    def test_pairs_whose_boxes_do_not_meet_are_not_intersected(self):
        hfss = FakeHfss([FakeBody("pcb_substrate", BOARD),
                         FakeBody("connector_1_dielectric", [-23, 1.416, -2, -19, 2.3, 2])])
        model_builder.dielectric_overlaps(hfss)
        self.assertEqual(hfss.modeler.intersected, [])

    def test_no_copy_is_left_behind(self):
        hfss = FakeHfss(
            [FakeBody("pcb_substrate", BOARD), FakeBody("intruder", [10, 1.1, -1, 12, 1.3, 1])],
            {("intruder", "pcb_substrate"): 0.8},
        )
        model_builder.dielectric_overlaps(hfss)
        self.assertEqual(sorted(hfss.modeler.object_names), ["intruder", "pcb_substrate"])


if __name__ == "__main__":
    unittest.main()
