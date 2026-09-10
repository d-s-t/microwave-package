"""Tests for where a driven run's ports go.

These are the numbers that fix the reference plane, so they are pinned
here against fakes rather than left to be discovered in AEDT. What the
fakes stand in for is small and well defined: a body with faces, a face
with edges and an area, and a modeller that records what it was asked to
build.

Run: python -m unittest discover -s tests
"""
import logging
import math
import unittest

from eigen_mode_analyze import copper_geometry as cg
from eigen_mode_analyze import ports, smp, solve_types


def setUpModule():
    # ports logs a line per port it builds, which is wanted in a real run
    # and is noise here. Importing webapp anywhere in the suite turns
    # INFO logging on process-wide, so this cannot be left to the default.
    logging.disable(logging.INFO)


def tearDownModule():
    logging.disable(logging.NOTSET)


# --- fakes ------------------------------------------------------------

class FakeEdge:
    def __init__(self, length):
        self.length = length


class FakeFace:
    """One face. `is_planar` is what tells the bead's flat ends from its
    curved walls, exactly as it does in PyAEDT."""

    def __init__(self, face_id, center, edges=(), area=None, is_planar=True):
        self.id = face_id
        self.center = list(center)
        self.edges = [FakeEdge(length) for length in edges]
        self.area = area
        self.is_planar = is_planar


class FakeObject:
    def __init__(self, faces):
        self.faces = list(faces)


class FakeModeler:
    def __init__(self, objects):
        self._objects = objects
        self.object_names = list(objects)
        self.rectangles = []

    def __getitem__(self, name):
        return self._objects[name]

    def create_rectangle(self, orientation, origin, sizes, name):
        self.rectangles.append(
            {"orientation": orientation, "origin": list(origin), "sizes": list(sizes), "name": name}
        )
        return type("Sheet", (), {"name": name})()


class FakeHfss:
    def __init__(self, objects):
        self.modeler = FakeModeler(objects)
        self.wave_ports = []
        self.lumped_ports = []

    def wave_port(self, **kwargs):
        self.wave_ports.append(kwargs)
        return True

    def lumped_port(self, **kwargs):
        self.lumped_ports.append(kwargs)
        return True


def annulus_faces(prefix_id, top_height, bottom_height, inner=0.45, outer=2.20):
    """A bead: two flat annular ends and a curved wall between them.

    The curved wall's centre sits at mid-height, which is what makes
    "the highest face centre" pick an end rather than the wall.
    """
    circumferences = (2 * math.pi * inner, 2 * math.pi * outer)
    area = math.pi * (outer ** 2 - inner ** 2)
    middle = (top_height + bottom_height) / 2.0
    return [
        FakeFace(prefix_id + 1, [0.0, top_height, 0.0], circumferences, area),
        FakeFace(prefix_id + 2, [0.0, bottom_height, 0.0], circumferences, area),
        FakeFace(prefix_id + 3, [0.0, middle, 0.0], (), None, is_planar=False),
    ]


def bead_model(top=1.5, bottom=0.95):
    return FakeHfss({
        "connector_1_dielectric": FakeObject(annulus_faces(100, top, bottom)),
        "connector_2_dielectric": FakeObject(annulus_faces(200, top, bottom)),
    })


class FakeLayout:
    def __init__(self, **values):
        self.frame = cg.Frame(x_offset=14.0, z_offset=6.0)
        self.stack = cg.Stack(
            pcb_bottom=1.0, pcb_top=1.381,
            chip_bottom=1.0, chip_top=1.381,
            copper_thickness=0.035, chip_metal_thickness=0.0002,
        )
        self.values = {
            "cpw_trace_width": 0.2, "pcb_length": 26.0,
            "port_space": 1.5, "port_count": 2, **values,
        }


DRIVEN = {"solution_type": solve_types.DRIVEN_MODAL, "port_impedance_ohm": 50.0}


# --- an eigenmode run has no ports ------------------------------------

class TestEigenmodeRunsGetNoPorts(unittest.TestCase):
    def test_no_ports_and_no_geometry_touched(self):
        hfss = bead_model()
        names = ports.place_ports(hfss, FakeLayout(), {"solution_type": solve_types.EIGENMODE})
        self.assertEqual(names, [])
        self.assertEqual(hfss.wave_ports, [])
        self.assertEqual(hfss.modeler.rectangles, [])

    def test_a_run_with_no_solution_type_is_one_too(self):
        self.assertEqual(ports.place_ports(bead_model(), FakeLayout(), {}), [])


# --- the connector reference plane ------------------------------------

class TestConnectorWavePorts(unittest.TestCase):
    SETTINGS = {**DRIVEN, "reference_plane": solve_types.REFERENCE_PLANE_CONNECTOR}

    def test_one_wave_port_per_bead_numbered_from_the_minus_x_end(self):
        """place_connectors calls the -x end connector 1, so port 1 is
        there too -- otherwise S21 would run the other way from run to
        run depending on nothing in particular."""
        hfss = bead_model()
        names = ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertEqual(names, ["port_1", "port_2"])
        self.assertEqual(len(hfss.wave_ports), 2)

    def test_the_port_goes_on_the_face_toward_the_mating_end(self):
        """The lower face's far side is the connector's SMD layer -- an
        off-axis centre tab and a void ring, not a coax cross-section."""
        hfss = bead_model(top=1.5, bottom=0.95)
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        # face id 101 is the top face of the first bead (see annulus_faces)
        self.assertEqual(hfss.wave_ports[0]["assignment"], 101)
        self.assertEqual(hfss.wave_ports[1]["assignment"], 201)

    def test_the_integration_line_crosses_the_annulus_radially(self):
        """A coax's TEM field is radial, so the line has to run from the
        centre conductor to the outer one -- not along the axis, and not
        from the middle of nowhere."""
        hfss = bead_model()
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        start, stop = hfss.wave_ports[0]["integration_line"]
        # Up is Y and the feedline is X, so radial here means Z.
        self.assertAlmostEqual(start[ports.UP], stop[ports.UP])
        self.assertAlmostEqual(start[ports.ALONG], stop[ports.ALONG])
        self.assertAlmostEqual(start[2], 0.45, places=6)
        self.assertAlmostEqual(stop[2], 2.20, places=6)

    def test_it_renormalises_to_the_runs_port_impedance(self):
        hfss = bead_model()
        ports.place_ports(hfss, FakeLayout(), {**self.SETTINGS, "port_impedance_ohm": 75.0})
        self.assertEqual(hfss.wave_ports[0]["impedance"], 75.0)
        self.assertTrue(hfss.wave_ports[0]["renormalize"])

    def test_a_model_with_no_bead_says_to_use_the_other_reference_plane(self):
        """The three-body split can fail -- place_connectors falls back to
        naming whatever arrived. There is then no coax cross-section, and
        the launch port does not need one."""
        hfss = FakeHfss({"connector_1": FakeObject([])})
        with self.assertRaises(ports.PortPlacementError) as caught:
            ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertIn("launch", str(caught.exception))

    def test_a_face_that_is_not_an_annulus_is_refused(self):
        """A wave port on it would still solve, and would not mean what
        the reference plane says it means."""
        faces = annulus_faces(100, 1.5, 0.95)
        faces[0].area = faces[0].area * 2  # edges and area no longer agree
        hfss = FakeHfss({"connector_1_dielectric": FakeObject(faces)})
        with self.assertRaises(ports.PortPlacementError) as caught:
            ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertIn("annulus", str(caught.exception))

    def test_a_bead_with_only_one_flat_end_is_refused(self):
        faces = annulus_faces(100, 1.5, 0.95)
        del faces[1]
        hfss = FakeHfss({"connector_1_dielectric": FakeObject(faces)})
        with self.assertRaises(ports.PortPlacementError):
            ports.place_ports(hfss, FakeLayout(), self.SETTINGS)


# --- the launch reference plane ---------------------------------------

class TestLaunchLumpedPorts(unittest.TestCase):
    SETTINGS = {**DRIVEN, "reference_plane": solve_types.REFERENCE_PLANE_LAUNCH}

    def test_one_sheet_and_one_port_per_end(self):
        hfss = bead_model()
        names = ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertEqual(names, ["port_1", "port_2"])
        self.assertEqual(len(hfss.modeler.rectangles), 2)
        self.assertEqual(len(hfss.lumped_ports), 2)

    def test_the_sheet_spans_the_substrate_and_the_trace_width(self):
        """Its horizontal edges have to land on copper -- the backing
        ground below, the trace above -- or it is not a lumped port."""
        hfss = bead_model()
        layout = FakeLayout()
        ports.place_ports(hfss, layout, self.SETTINGS)
        sheet = hfss.modeler.rectangles[0]
        substrate = layout.stack.pcb_top - layout.stack.pcb_bottom
        # WhichAxis="X" puts width along Y and height along Z.
        self.assertEqual(sheet["orientation"], "X")
        self.assertAlmostEqual(sheet["sizes"][0], substrate)
        self.assertAlmostEqual(sheet["sizes"][1], layout.values["cpw_trace_width"])
        self.assertAlmostEqual(sheet["origin"][1], layout.stack.pcb_bottom)

    def test_the_sheet_is_centred_across_the_trace(self):
        hfss = bead_model()
        layout = FakeLayout()
        ports.place_ports(hfss, layout, self.SETTINGS)
        low_edge = hfss.modeler.rectangles[0]["origin"][2]
        trace = layout.values["cpw_trace_width"]
        self.assertAlmostEqual(low_edge + trace / 2.0, layout.frame.z_offset)

    def test_the_ports_sit_just_inboard_of_the_land_pattern(self):
        """Nothing of the launch reaches past launch_inner_edge, so a
        port there is clear of the connector's own copper."""
        hfss = bead_model()
        layout = FakeLayout()
        ports.place_ports(hfss, layout, self.SETTINGS)
        inner = smp.launch_inner_edge(
            layout.values["pcb_length"] / 2.0, layout.values["port_space"]
        )
        xs = sorted(r["origin"][0] for r in hfss.modeler.rectangles)
        self.assertAlmostEqual(xs[0], layout.frame.x_offset - inner)
        self.assertAlmostEqual(xs[1], layout.frame.x_offset + inner)

    def test_the_integration_line_runs_ground_to_trace(self):
        hfss = bead_model()
        layout = FakeLayout()
        ports.place_ports(hfss, layout, self.SETTINGS)
        start, stop = hfss.lumped_ports[0]["integration_line"]
        self.assertAlmostEqual(start[ports.UP], layout.stack.pcb_bottom)
        self.assertAlmostEqual(stop[ports.UP], layout.stack.pcb_top)
        self.assertLess(start[ports.UP], stop[ports.UP])

    def test_a_geometry_with_one_port_is_refused(self):
        hfss = bead_model()
        with self.assertRaises(ports.PortPlacementError) as caught:
            ports.place_ports(hfss, FakeLayout(port_count=1), self.SETTINGS)
        self.assertIn("S21", str(caught.exception))


# --- things that apply either way -------------------------------------

class TestWhenThereIsNothingToPortOnto(unittest.TestCase):
    def test_a_run_with_no_layout_is_refused(self):
        """No copper.txt means no connectors were placed and no trace was
        drawn, so neither reference plane has anything to attach to."""
        with self.assertRaises(ports.PortPlacementError) as caught:
            ports.place_ports(bead_model(), None, {**DRIVEN})
        self.assertIn("no layout", str(caught.exception))

    def test_an_unknown_reference_plane_is_refused(self):
        with self.assertRaises(ports.PortPlacementError):
            ports.place_ports(
                bead_model(), FakeLayout(), {**DRIVEN, "reference_plane": "nowhere"}
            )


if __name__ == "__main__":
    unittest.main()
