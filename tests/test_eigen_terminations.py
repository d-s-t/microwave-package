"""Tests for the connector termination an Eigenmode run can ask for.

With `connector_termination_ohm` set, each SMP connector's bore is left open
at its top and an impedance boundary goes on that face, in place of HFSS's
default perfect-E outer boundary, which shorts the pin to its shell. These
are pinned against fakes in the style of tests/test_ports.py; what HFSS makes
of the boundary is only seen in a real run.

Run: python -m unittest discover -s tests -p test_eigen_terminations.py
"""
import logging
import math
import tempfile
import unittest

from werkzeug.datastructures import MultiDict

from microwave_package import paths, pipeline, webapp
from microwave_package.hfss import registry
from microwave_package.hfss.driven import ports
from microwave_package.hfss.eigenmode import EIGENMODE, terminations


def setUpModule():
    # The termination logs a line per connector, and importing webapp turns
    # INFO logging on process-wide, so it is silenced for this module.
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _quiet(message):
    pass


# --- fakes ------------------------------------------------------------

class FakeEdge:
    def __init__(self, length):
        self.length = length


class FakeFace:
    """One face. `is_planar` tells a bore's flat ends from its wall, as it does in PyAEDT."""

    def __init__(self, face_id, center, edges=(), area=None, is_planar=True):
        self.id = face_id
        self.center = list(center)
        self.edges = [FakeEdge(length) for length in edges]
        self.area = area
        self.is_planar = is_planar


class FakeBody:
    def __init__(self, faces=(), name=None, material=None):
        self.faces = list(faces)
        self.name = name
        self.material = material
        self.solve_inside = True


def annulus_faces(prefix, top, bottom, along, across, inner, outer):
    """Two flat annular ends and a curved wall between them."""
    circumferences = (2 * math.pi * inner, 2 * math.pi * outer)
    area = math.pi * (outer ** 2 - inner ** 2)
    return [
        FakeFace(prefix + 1, [along, top, across], circumferences, area),
        FakeFace(prefix + 2, [along, bottom, across], circumferences, area),
        FakeFace(prefix + 3, [along, (top + bottom) / 2.0, across], (), None, is_planar=False),
    ]


class FakeModeler:
    def __init__(self, objects):
        self._objects = dict(objects)
        self.object_names = list(objects)
        self.cylinders = []
        self.subtractions = []

    def __getitem__(self, name):
        return self._objects[name]

    def create_cylinder(self, orientation, origin, radius, height, name, material):
        # Left as the annulus between the pin and the bore wall, the way the
        # cylinder is once the conductors are cut out of it.
        self.cylinders.append(name)
        along, bottom, across = origin
        faces = annulus_faces(1000 * len(self.cylinders), bottom + height, bottom, along, across,
                              inner=0.30, outer=1.00)
        body = FakeBody(faces, name=name, material=material)
        self._objects[name] = body
        self.object_names.append(name)
        return body

    def subtract(self, blank, tools, keep_originals):
        self.subtractions.append((blank, list(tools)))
        return True


class FakeOBoundary:
    """The native boundary module. `outcome` is what AssignImpedance returns, or an exception it raises."""

    def __init__(self):
        self.calls = []
        self.outcome = None

    def AssignImpedance(self, args):
        self.calls.append(args)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeHfss:
    def __init__(self, objects):
        self.modeler = FakeModeler(objects)
        self.oboundary = FakeOBoundary()
        self.impedances = []
        # What PyAEDT's helper does: True when it made the boundary, False when
        # it did not, or an exception it raises.
        self.helper_outcome = True

    def assign_impedance_to_sheet(self, assignment, name=None, resistance=50.0, reactance=0.0,
                                  is_infinite_ground=False, coordinate_system="Global"):
        self.impedances.append({
            "assignment": assignment, "name": name, "resistance": resistance,
            "reactance": reactance, "is_infinite_ground": is_infinite_ground,
        })
        if isinstance(self.helper_outcome, Exception):
            raise self.helper_outcome
        return self.helper_outcome


# Where the two connectors' axes sit, port 1 at the -x end.
AXES = {1: (-12.5, 0.3), 2: (12.5, 0.3)}
BEAD_TOP = 1.5
PAIR_BORES = ["connector_term_a_bore", "connector_term_b_bore"]
SETTINGS = {"connector_termination_ohm": 50.0}


def bead_model():
    objects = {"vacuum": FakeBody()}
    for index, (along, across) in AXES.items():
        objects[f"connector_{index}_dielectric"] = FakeBody(annulus_faces(
            100 * index, BEAD_TOP, 0.95, along, across, inner=0.45, outer=2.20))
        objects[f"connector_{index}_center"] = FakeBody()
        objects[f"connector_{index}_outer"] = FakeBody()
    return FakeHfss(objects)


def bore_top(hfss, bore):
    """The bore's one flat face at its top: the bead's top plus the bore's height."""
    (face,) = [
        f for f in hfss.modeler[bore].faces
        if f.is_planar and abs(f.center[1] - (BEAD_TOP + ports.BORE_PORT_HEIGHT)) < 1e-9
    ]
    return face


# --- off, and refused -------------------------------------------------

class TestOff(unittest.TestCase):
    def test_zero_or_no_setting_builds_nothing(self):
        hfss = bead_model()
        for settings in ({"connector_termination_ohm": 0.0}, {}):
            self.assertEqual(terminations.place_terminations(hfss, object(), settings, log=_quiet), [])
        self.assertEqual(hfss.modeler.cylinders, [])
        self.assertEqual(hfss.impedances, [])
        self.assertEqual(hfss.oboundary.calls, [])

    def test_no_layout_warns_and_builds_nothing(self):
        hfss = bead_model()
        lines = []
        self.assertEqual(terminations.place_terminations(hfss, None, SETTINGS, log=lines.append), [])
        self.assertTrue(any(line.startswith("WARNING") for line in lines))
        self.assertEqual(hfss.modeler.cylinders, [])
        self.assertEqual(hfss.impedances, [])

    def test_a_negative_resistance_is_refused(self):
        with self.assertRaises(registry.PortPlacementError):
            terminations.place_terminations(
                bead_model(), object(), {"connector_termination_ohm": -50.0}, log=_quiet)


# --- the impedance boundary -------------------------------------------

class TestSurfaceImpedance(unittest.TestCase):
    def test_50_ohm_on_a_0_3_mm_pin_in_a_1_mm_bore_is_260_9_ohm_per_square(self):
        # The brief quoted 261.1 for these radii; the formula gives 260.94.
        self.assertAlmostEqual(terminations.surface_impedance(50.0, 0.3, 1.0), 260.94, places=1)

    def test_a_72_2_ohm_load_is_377_ohm_per_square(self):
        self.assertEqual(round(terminations.surface_impedance(72.2, 0.3, 1.0)), 377)


class TestTheImpedanceBoundary(unittest.TestCase):
    def test_one_boundary_per_connector_on_its_own_bore_top(self):
        hfss = bead_model()
        self.assertEqual(terminations.place_terminations(hfss, object(), SETTINGS, log=_quiet), [])
        self.assertEqual(
            [call["name"] for call in hfss.impedances], ["connector_term_a", "connector_term_b"])
        zs = terminations.surface_impedance(50.0, 0.3, 1.0)
        for call, bore, index in zip(hfss.impedances, PAIR_BORES, (1, 2)):
            (face,) = call["assignment"]
            self.assertIs(face, bore_top(hfss, bore))
            self.assertEqual(face.center[0], AXES[index][0])
            self.assertAlmostEqual(call["resistance"], zs)
            self.assertEqual(call["reactance"], 0.0)
            self.assertFalse(call["is_infinite_ground"])
        self.assertEqual(hfss.oboundary.calls, [])

    def test_the_native_call_is_used_when_pyaedt_returns_false(self):
        hfss = bead_model()
        hfss.helper_outcome = False
        terminations.place_terminations(hfss, object(), SETTINGS, log=_quiet)
        self.assertEqual([args[0] for args in hfss.oboundary.calls],
                         ["NAME:connector_term_a", "NAME:connector_term_b"])
        args = hfss.oboundary.calls[0]
        self.assertEqual(args[1:3], ["Faces:=", [bore_top(hfss, PAIR_BORES[0]).id]])
        self.assertEqual(args[3], "Resistance:=")
        self.assertAlmostEqual(float(args[4]), terminations.surface_impedance(50.0, 0.3, 1.0))
        self.assertEqual(args[5:], ["Reactance:=", "0.0", "InfGroundPlane:=", False])

    def test_the_native_call_is_used_when_pyaedt_raises(self):
        hfss = bead_model()
        hfss.helper_outcome = RuntimeError("the helper refused")
        terminations.place_terminations(hfss, object(), SETTINGS, log=_quiet)
        self.assertEqual(len(hfss.oboundary.calls), 2)

    def test_both_failing_refuses_the_model(self):
        hfss = bead_model()
        hfss.helper_outcome = False
        hfss.oboundary.outcome = RuntimeError("HFSS refused")
        with self.assertRaises(registry.PortPlacementError):
            terminations.place_terminations(hfss, object(), SETTINGS, log=_quiet)

    def test_a_native_call_that_returns_false_refuses_the_model(self):
        hfss = bead_model()
        hfss.helper_outcome = False
        hfss.oboundary.outcome = False
        with self.assertRaises(registry.PortPlacementError):
            terminations.place_terminations(hfss, object(), SETTINGS, log=_quiet)


# --- the bore, with and without its cap -------------------------------

class TestTheOpenBore(unittest.TestCase):
    def test_without_a_cap_the_bore_is_the_only_body(self):
        hfss = bead_model()
        air = ports._bore_section(hfss, "connector_1_dielectric", "connector_1_center", "vacuum",
                                  "connector_term_a", log=_quiet, cap=False)
        self.assertEqual(air, "connector_term_a_bore")
        self.assertEqual(hfss.modeler.cylinders, ["connector_term_a_bore"])
        self.assertNotIn("connector_term_a_cap", hfss.modeler.object_names)
        self.assertEqual(hfss.modeler.subtractions[-1], ("vacuum", ["connector_term_a_bore"]))

    def test_the_driven_build_still_closes_the_bore_with_a_cap(self):
        hfss = bead_model()
        ports._bore_section(hfss, "connector_1_dielectric", "connector_1_center", "vacuum",
                            "connector_load_a", log=_quiet)
        self.assertEqual(hfss.modeler.cylinders, ["connector_load_a_bore", "connector_load_a_cap"])
        self.assertEqual(hfss.modeler.subtractions[-1],
                         ("vacuum", ["connector_load_a_bore", "connector_load_a_cap"]))


# --- where the setting comes from and goes ----------------------------

class TestTheSetting(unittest.TestCase):
    def test_the_eigenmode_solver_places_the_terminations(self):
        self.assertIs(registry.get(EIGENMODE).place_ports, terminations.place_terminations)

    def test_eigenmode_is_still_the_default_solver(self):
        # Importing the driven package would register it first and make it the
        # default, which is why terminations imports it inside the function.
        self.assertEqual(registry.default_solution_type(), EIGENMODE)

    def test_it_defaults_to_off(self):
        self.assertEqual(registry.all_defaults()["connector_termination_ohm"], 0.0)

    def test_the_form_reads_it_for_an_eigenmode_submission(self):
        settings = webapp._solve_settings_from_form(MultiDict(
            [("solution_type", EIGENMODE), ("connector_termination_ohm", "50")]))
        self.assertEqual(settings["connector_termination_ohm"], 50.0)

    def test_a_driven_submission_does_not_take_it(self):
        settings = webapp._solve_settings_from_form(MultiDict(
            [("solution_type", "DrivenModal"), ("connector_termination_ohm", "50")]))
        self.assertEqual(settings["connector_termination_ohm"], 0.0)

    def test_the_command_line_sets_it_with_setting(self):
        settings = pipeline.apply_setting_overrides({}, ["connector_termination_ohm=50"])
        self.assertEqual(settings["connector_termination_ohm"], 50.0)


class TestTheRunForm(unittest.TestCase):
    def setUp(self):
        # Rendering the form reads the runs folder, so it is pointed at a temporary one.
        saved = paths.RUNS_ROOT
        paths.RUNS_ROOT = tempfile.mkdtemp(prefix="runs_")
        self.addCleanup(setattr, paths, "RUNS_ROOT", saved)

    def test_the_form_offers_it(self):
        page = webapp.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('name="connector_termination_ohm"', page)


if __name__ == "__main__":
    unittest.main()
