"""Tests for the bare-board option (`no_chip = 1`).

The option builds only the PCB and its connectors -- a straight CPW from
one SMP to the other, with no die, opening, pedestal, bond pads or wire
bonds -- so that board and connector behaviour can be told apart from the
chip's. Everything here is code generation and fakes; nothing opens
Altium, SolidWorks or AEDT.

Run: python -m unittest discover -s tests
"""
import logging
import unittest

from microwave_package import paths
from microwave_package.design import (
    altium_generation as ag, bonds, cavity, constraints, copper_geometry as cg,
    equations as eq, parameter_meta,
)
from microwave_package.hfss import registry
from microwave_package.hfss.driven import DRIVEN_MODAL, ports
from microwave_package.hfss.registry import PortPlacementError

from test_copper_geometry import FRAME, STACK, FakeModeler
from test_ports import FakeLayout, bead_model


def setUpModule():
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


def defaults():
    return eq.get_numeric_parameters(eq.read_file(str(paths.DEFAULT_EQUATIONS_FILE)))


def bare(**overrides):
    return dict(defaults(), no_chip=1, **overrides)


class TestTheParameter(unittest.TestCase):
    def test_it_is_off_by_default(self):
        self.assertEqual(defaults()["no_chip"], 0)
        self.assertFalse(parameter_meta.is_bare_board(defaults()))

    def test_it_is_a_flag_not_a_swept_dimension(self):
        self.assertIn(parameter_meta.NO_CHIP_PARAM, parameter_meta.BOOLEAN_PARAMETERS)
        listed = [n for g in parameter_meta.grouped(defaults()) for n in g["names"]]
        self.assertNotIn("no_chip", listed)

    def test_it_reads_as_on_for_one_and_off_for_zero_or_absent(self):
        self.assertTrue(parameter_meta.is_bare_board({"no_chip": 1}))
        self.assertTrue(parameter_meta.is_bare_board({"no_chip": 1.0}))
        self.assertFalse(parameter_meta.is_bare_board({"no_chip": 0}))
        self.assertFalse(parameter_meta.is_bare_board({}))


class TestNothingOfTheChipIsPlanned(unittest.TestCase):
    def test_there_is_no_opening(self):
        opening = cavity.plan(bare())
        self.assertTrue(opening.is_empty)
        self.assertEqual((opening.length, opening.side), (0.0, 0.0))

    def test_altium_is_told_the_opening_has_no_size(self):
        """Zero is how the DelphiScript reads "no opening": the centre
        conductor runs unbroken and the planes stay whole."""
        params = ag.build_params(bare())
        self.assertEqual(params["cavity_length"], 0.0)
        self.assertEqual(params["cavity_side"], 0.0)
        self.assertEqual(params["no_chip"], 1)

    def test_there_are_no_bonds(self):
        gap = ag.build_params(bare())["pcb_cpw_gap"]
        self.assertEqual(bonds.layout(bare(), gap), [])

    def test_the_default_still_plans_a_chip_and_bonds(self):
        v = defaults()
        self.assertFalse(cavity.plan(v).is_empty)
        self.assertGreater(len(bonds.layout(v, ag.build_params(v)["pcb_cpw_gap"])), 0)

    def test_the_board_fingerprint_changes_with_the_flag(self):
        """Two boards that differ only in no_chip are different boards,
        so one must never be reused as the other."""
        a = ag.board_fingerprint(defaults())
        b = ag.board_fingerprint(bare())
        if a is None or b is None:
            self.skipTest("the generator script is not readable here")
        self.assertNotEqual(a, b)


class TestConstraints(unittest.TestCase):
    def test_a_board_with_no_room_for_a_chip_is_accepted(self):
        """Shorten the board until the die's bond fingers would run into
        the SMP launch, which only the chip cares about."""
        small = dict(pcb_length=30.0)
        self.assertIsNotNone(constraints.check_layout(dict(defaults(), **small)))
        self.assertIsNone(constraints.check_layout(bare(**small)))

    def test_the_default_board_is_still_buildable_without_a_chip(self):
        self.assertIsNone(constraints.check_layout(bare()))

    def test_a_bare_board_does_not_reserve_chip_room_for_the_distortion(self):
        tight = dict(no_distortion=0, pcb_length=30.0, distortion_radius=1.0,
                     distortion_x=0.0)
        self.assertIsNotNone(constraints.check_combination(dict(defaults(), **tight)))
        self.assertIsNone(constraints.check_combination(bare(**tight)))

    def test_the_via_fence_is_still_checked(self):
        """Not everything is waived: the fence needs the board's width."""
        self.assertIn("via fence", constraints.check_layout(bare(pcb_side=1.0)))

    def test_the_connectors_still_have_to_fit(self):
        self.assertIsNotNone(constraints.check_layout(bare(pcb_length=20.0)))


class TestTheScriptsReport(unittest.TestCase):
    BARE_REPORT = {
        "no_chip": "1", "package_body": "ok", "package_cavity": "skipped",
        "package_ports": "2", "package_bond_pads": "skipped",
        "chip_body": "skipped", "chip_bond_pads": "skipped",
    }

    def test_skipped_chip_features_are_not_a_failure(self):
        self.assertEqual(ag._missing_features(self.BARE_REPORT), "")

    def test_a_bare_board_still_needs_its_copper_and_connectors(self):
        self.assertIn("copper", ag._missing_features(dict(self.BARE_REPORT, package_body="FAIL")))
        self.assertIn("connector", ag._missing_features(
            dict(self.BARE_REPORT, package_ports="NO_LAUNCH_PLACED")))

    def test_an_ordinary_board_is_still_held_to_its_chip_features(self):
        report = {k: v for k, v in self.BARE_REPORT.items()
                  if k not in ("no_chip", "chip_body")}
        self.assertIn("never reported on", ag._missing_features(report))


class TestTheSubstrates(unittest.TestCase):
    def test_only_the_board_is_built_and_it_is_not_pocketed(self):
        modeler = FakeModeler()
        made = cg.build_substrates(
            modeler, FRAME, STACK, 25, 12.5, 0.0, 0.0, cavity.plan(bare()),
            log=lambda _m: None,
        )
        self.assertEqual(made, ["pcb_substrate"])
        self.assertEqual(len(modeler.boxes), 1)
        self.assertEqual(modeler.subtractions, [])

    def test_clearing_the_opening_out_of_the_copper_does_nothing(self):
        modeler = FakeModeler()
        cg.clear_cavity_area(modeler, FRAME, STACK, cavity.plan(bare()),
                             targets=["pcb_copper_top"], log=lambda _m: None)
        self.assertEqual(modeler.boxes, [])
        self.assertEqual(modeler.subtractions, [])


class TestPorts(unittest.TestCase):
    def layout(self):
        return FakeLayout(no_chip=1)

    def test_the_connector_plane_still_gives_two_ports(self):
        settings = {"solution_type": DRIVEN_MODAL, "port_impedance_ohm": 50.0,
                    "reference_plane": ports.REFERENCE_PLANE_CONNECTOR}
        names = ports.place_ports(bead_model(), self.layout(), settings)
        self.assertEqual(names, ["port_1", "port_2"])

    def test_the_planes_that_sit_at_the_bonds_are_refused_plainly(self):
        for plane in (ports.REFERENCE_PLANE_BONDS, ports.REFERENCE_PLANE_CHIP):
            settings = {"solution_type": DRIVEN_MODAL, "port_impedance_ohm": 50.0,
                        "reference_plane": plane}
            with self.assertRaisesRegex(PortPlacementError, "no_chip"):
                ports.place_ports(bead_model(), self.layout(), settings)


if __name__ == "__main__":
    unittest.main()
