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

from microwave_package.design import copper_geometry as cg
from microwave_package.design import smp
from microwave_package.hfss import registry
from microwave_package.hfss.driven import DRIVEN_MODAL, ports


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
    def __init__(self, faces, name=None, material=None):
        self.faces = list(faces)
        self.name = name
        self.material = material
        self.solve_inside = True


class FakeModeler:
    # What a cylinder in the bore is left as once the pin and the bore
    # wall are subtracted from it: the annulus between them.
    BORE_INNER = 0.30
    BORE_OUTER = 1.00

    def __init__(self, objects):
        self._objects = objects
        self.object_names = list(objects)
        self.rectangles = []
        self.cylinders = []
        self.subtractions = []
        # Set to make the bore's cut-out faces disagree with their edges.
        self.bore_area_scale = 1.0

    def __getitem__(self, name):
        return self._objects[name]

    def create_cylinder(self, orientation, origin, radius, height, name, material):
        self.cylinders.append({
            "orientation": orientation, "origin": list(origin), "radius": radius,
            "height": height, "name": name, "material": material,
        })
        along, bottom, across = origin
        faces = annulus_faces(1000 * (len(self.cylinders) + 1), bottom + height, bottom,
                              inner=self.BORE_INNER, outer=self.BORE_OUTER,
                              along=along, across=across)
        for face in faces[:2]:
            if face.area:
                face.area *= self.bore_area_scale
        body = FakeObject(faces, name=name, material=material)
        self._objects[name] = body
        self.object_names.append(name)
        return body

    def subtract(self, blank, tools, keep_originals):
        self.subtractions.append((blank, list(tools), keep_originals))
        return True

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


def annulus_faces(prefix_id, top_height, bottom_height, inner=0.45, outer=2.20,
                  along=0.0, across=0.0):
    """Two flat annular ends and a curved wall between them.

    The curved wall's centre sits at mid-height, which is what makes
    "the highest face centre" pick an end rather than the wall.
    """
    circumferences = (2 * math.pi * inner, 2 * math.pi * outer)
    area = math.pi * (outer ** 2 - inner ** 2)
    middle = (top_height + bottom_height) / 2.0
    return [
        FakeFace(prefix_id + 1, [along, top_height, across], circumferences, area),
        FakeFace(prefix_id + 2, [along, bottom_height, across], circumferences, area),
        FakeFace(prefix_id + 3, [along, middle, across], (), None, is_planar=False),
    ]


# Where the two connectors' axes sit, port 1 at the -x end.
AXES = {1: (-12.5, 0.3), 2: (12.5, 0.3)}


def bead_model(top=1.5, bottom=0.95):
    objects = {}
    for index, (along, across) in AXES.items():
        objects[f"connector_{index}_dielectric"] = FakeObject(
            annulus_faces(100 * index, top, bottom, along=along, across=across))
        objects[f"connector_{index}_center"] = FakeObject([])
        objects[f"connector_{index}_outer"] = FakeObject([])
    objects["vacuum"] = FakeObject([])
    return FakeHfss(objects)


class FakeLayout:
    def __init__(self, **values):
        self.frame = cg.Frame(x_offset=14.0, z_offset=6.0)
        self.stack = cg.Stack(
            pcb_bottom=1.0, pcb_top=1.381,
            chip_bottom=1.0, chip_top=1.381,
            copper_thickness=0.035, chip_metal_thickness=0.0002,
        )
        # Half the die's trace plus its gap, the way the layout measures it.
        self.chip_ground_offset = 0.26981
        self.values = {
            "cpw_trace_width": 0.2, "pcb_length": 26.0,
            "port_space": 1.5, "port_count": 2, **values,
        }


DRIVEN = {"solution_type": DRIVEN_MODAL, "port_impedance_ohm": 50.0}


# --- an eigenmode run has no ports ------------------------------------

class TestEigenmodeRunsGetNoPorts(unittest.TestCase):
    """The eigenmode solver's `place_ports` is the connector termination, which
    is off unless `connector_termination_ohm` is set (see
    tests/test_eigen_terminations.py), so with the default it builds nothing."""

    def test_the_eigenmode_solver_builds_nothing(self):
        hfss = bead_model()
        names = registry.get("Eigenmode").place_ports(hfss, FakeLayout(), {}, None)
        self.assertEqual(names, [])
        self.assertEqual(hfss.wave_ports, [])
        self.assertEqual(hfss.modeler.rectangles, [])

    def test_a_run_with_no_solution_type_is_an_eigenmode_run(self):
        solver = registry.for_settings({})
        self.assertEqual(solver.place_ports(bead_model(), FakeLayout(), {}, None), [])


# --- the connector reference plane ------------------------------------

class TestConnectorWavePorts(unittest.TestCase):
    SETTINGS = {**DRIVEN, "reference_plane": ports.REFERENCE_PLANE_CONNECTOR}

    def test_one_wave_port_per_bead_numbered_from_the_minus_x_end(self):
        """place_connectors calls the -x end connector 1, so port 1 is
        there too -- otherwise S21 would run the other way from run to
        run depending on nothing in particular."""
        hfss = bead_model()
        names = ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertEqual(names, ["port_1", "port_2"])
        self.assertEqual(len(hfss.wave_ports), 2)

    def test_an_air_bore_and_a_pec_cap_stack_on_the_bead_top(self):
        """The model's vacuum stops at the box ceiling, below the bead's
        top, so the bore above it has to be added to have a coax to put a
        port on -- and closed, so the port is on the solved region's
        outside."""
        hfss = bead_model(top=1.5, bottom=0.95)
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        bore, cap = hfss.modeler.cylinders[:2]
        self.assertEqual((bore["name"], bore["material"]), ("port_1_bore", "vacuum"))
        self.assertEqual((cap["name"], cap["material"]), ("port_1_cap", "pec"))
        self.assertAlmostEqual(bore["origin"][ports.UP], 1.5)
        self.assertAlmostEqual(cap["origin"][ports.UP], 1.5 + ports.BORE_PORT_HEIGHT)
        self.assertTrue(hfss.modeler["port_1_bore"].solve_inside)
        self.assertFalse(hfss.modeler["port_1_cap"].solve_inside)

    def test_the_bore_is_centred_on_the_connector_axis(self):
        """Not on a bounding box: the centre conductor's runs out along
        its SMD tab. The bead top's annular face is on the axis."""
        hfss = bead_model()
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        for cylinder, index in zip(hfss.modeler.cylinders[::2], (1, 2)):
            along, across = AXES[index]
            self.assertAlmostEqual(cylinder["origin"][ports.ALONG], along)
            self.assertAlmostEqual(cylinder["origin"][2], across)

    def test_both_bodies_are_cut_to_the_conductors_and_out_of_the_vacuum(self):
        hfss = bead_model()
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        subtractions = hfss.modeler.subtractions
        self.assertIn(("port_1_bore", ["connector_1_center", "connector_1_outer"], True), subtractions)
        self.assertIn(("port_1_cap", ["connector_1_center", "connector_1_outer"], True), subtractions)
        self.assertIn(("vacuum", ["port_1_bore", "port_1_cap"], True), subtractions)

    def test_the_port_goes_on_top_of_the_air_bore(self):
        """Its top face is where the cap sits: the solved region's edge."""
        hfss = bead_model(top=1.5, bottom=0.95)
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        # Cylinders are numbered in creation order (bore, cap, bore, cap);
        # face +1 is each one's top (see annulus_faces).
        self.assertEqual(hfss.wave_ports[0]["assignment"], 2001)
        self.assertEqual(hfss.wave_ports[1]["assignment"], 4001)

    def test_the_integration_line_crosses_the_annulus_radially(self):
        """A coax's TEM field is radial, so the line has to run from the
        centre conductor to the outer one -- not along the axis, and not
        from the middle of nowhere."""
        hfss = bead_model()
        ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        start, stop = hfss.wave_ports[0]["integration_line"]
        across = AXES[1][1]
        # Up is Y and the feedline is X, so radial here means Z.
        self.assertAlmostEqual(start[ports.UP], stop[ports.UP])
        self.assertAlmostEqual(start[ports.ALONG], stop[ports.ALONG])
        self.assertAlmostEqual(start[2], across + 0.30, places=6)
        self.assertAlmostEqual(stop[2], across + 1.00, places=6)

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
        hfss = bead_model()
        hfss.modeler.bore_area_scale = 2.0  # edges and area no longer agree
        with self.assertRaises(ports.PortPlacementError) as caught:
            ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertIn("annulus", str(caught.exception))

    def test_a_bead_with_only_one_flat_end_is_refused(self):
        hfss = bead_model()
        del hfss.modeler["connector_1_dielectric"].faces[1]
        with self.assertRaises(ports.PortPlacementError):
            ports.place_ports(hfss, FakeLayout(), self.SETTINGS)

    def test_a_bead_top_with_no_annular_face_is_refused(self):
        """Then there is nothing to find the axis from, and a bore drawn
        off-axis would cut into the pin or the wall."""
        hfss = bead_model()
        hfss.modeler["connector_1_dielectric"].faces[0].area *= 2
        with self.assertRaises(ports.PortPlacementError) as caught:
            ports.place_ports(hfss, FakeLayout(), self.SETTINGS)
        self.assertIn("axis", str(caught.exception))


# --- the launch reference plane ---------------------------------------

class TestLaunchLumpedPorts(unittest.TestCase):
    SETTINGS = {**DRIVEN, "reference_plane": ports.REFERENCE_PLANE_LAUNCH}

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
        # The YZ plane's sizes run along Y (up) then Z (across).
        self.assertEqual(sheet["orientation"], "YZ")
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


# --- the bond-finger and chip reference planes --------------------------

class TestPortsNextToTheBonds(unittest.TestCase):
    def _run(self, plane):
        hfss = bead_model()
        layout = FakeLayout(chip_length=6.3, chip_side=6.3, bonding_clearence=0.6)
        names = ports.place_ports(hfss, layout, {**DRIVEN, "reference_plane": plane})
        return hfss, layout, names

    def test_the_bond_finger_ports_sit_outboard_of_the_fingers(self):
        hfss, layout, _ = self._run(ports.REFERENCE_PLANE_BONDS)
        xs = sorted(r["origin"][0] - layout.frame.x_offset for r in hfss.modeler.rectangles)
        # fingers at 3.5 mm, 0.3 mm long
        self.assertAlmostEqual(xs[0], -(3.5 + 0.15 + ports.PAD_SETBACK))
        self.assertAlmostEqual(xs[1], 3.5 + 0.15 + ports.PAD_SETBACK)

    def test_the_bond_finger_ports_are_ordinary_lumped_ports(self):
        hfss, _, names = self._run(ports.REFERENCE_PLANE_BONDS)
        self.assertEqual(names, ["port_1", "port_2"])
        self.assertEqual(len(hfss.lumped_ports), 2)


class TestChipGapPorts(unittest.TestCase):
    """The die has no backing ground, so its port goes from the trace to
    the coplanar ground, across each gap."""

    def _run(self, **values):
        hfss = bead_model()
        layout = FakeLayout(chip_length=6.3, chip_side=6.3, bonding_clearence=0.6, **values)
        names = ports.place_ports(
            hfss, layout, {**DRIVEN, "reference_plane": ports.REFERENCE_PLANE_CHIP}
        )
        return hfss, layout, names

    def test_two_ends_of_two_gap_ports_each(self):
        hfss, _, names = self._run()
        self.assertEqual(names, ["port_1", "port_2"])
        self.assertEqual(
            [p["name"] for p in hfss.lumped_ports],
            ["port_1_a", "port_1_b", "port_2_a", "port_2_b"],
        )

    def test_the_ports_sit_inboard_of_the_die_pads(self):
        hfss, layout, _ = self._run()
        # die pads centred on 2.85 mm, 0.3 mm long; each sheet is centred on the port plane
        gap = layout.chip_ground_offset - 0.1
        centres = sorted({round(r["origin"][0] - layout.frame.x_offset + gap / 2.0, 6)
                          for r in hfss.modeler.rectangles})
        self.assertAlmostEqual(centres[0], -(2.85 - 0.15 - ports.PAD_SETBACK), places=5)
        self.assertAlmostEqual(centres[1], 2.85 - 0.15 - ports.PAD_SETBACK, places=5)

    def test_each_sheet_is_the_gap_itself_flat_on_the_die(self):
        """From the trace's edge to the ground's edge, in the die's top
        plane: both its long edges land on copper."""
        hfss, layout, _ = self._run()
        gap = layout.chip_ground_offset - 0.1
        sheet = hfss.modeler.rectangles[0]
        self.assertEqual(sheet["orientation"], "XZ")
        self.assertAlmostEqual(sheet["sizes"][0], gap)
        # half a film's thickness up, so it meets a solid film's sides (none for a sheet)
        self.assertAlmostEqual(
            sheet["origin"][ports.UP],
            layout.stack.chip_top + layout.stack.chip_metal_thickness / 2.0,
        )
        spans = sorted((r["origin"][2] - layout.frame.z_offset) for r in hfss.modeler.rectangles[:2])
        # one gap each side of the trace: -0.2698..-0.1 and +0.1..+0.2698 across
        self.assertAlmostEqual(spans[0], -layout.chip_ground_offset)
        self.assertAlmostEqual(spans[1], 0.1)

    def test_both_integration_lines_run_from_the_ground_to_the_trace(self):
        """Opposite ways in space, the same way relative to the trace --
        which is the even mode a CPW carries."""
        hfss, layout, _ = self._run()
        for port in hfss.lumped_ports[:2]:
            start, stop = port["integration_line"]
            self.assertAlmostEqual(start[ports.UP], stop[ports.UP])
            # starts at the ground's edge, ends at the trace's
            self.assertAlmostEqual(abs(start[2] - layout.frame.z_offset), layout.chip_ground_offset)
            self.assertAlmostEqual(abs(stop[2] - layout.frame.z_offset), 0.1)
        first, second = (p["integration_line"] for p in hfss.lumped_ports[:2])
        self.assertGreater((first[1][2] - first[0][2]) * (second[1][2] - second[0][2]) * -1, 0)

    def test_each_gap_port_is_twice_the_run_impedance(self):
        """Two in parallel are one port of the run's impedance."""
        hfss, _, _ = self._run()
        self.assertTrue(all(p["impedance"] == 100.0 for p in hfss.lumped_ports))

    def test_a_die_with_no_copper_is_refused(self):
        hfss = bead_model()
        layout = FakeLayout()
        layout.chip_ground_offset = None
        with self.assertRaises(ports.PortPlacementError):
            ports.place_ports(hfss, layout, {**DRIVEN, "reference_plane": ports.REFERENCE_PLANE_CHIP})


class TestConnectorTerminations(unittest.TestCase):
    """Every plane but the connector's leaves the connectors matched:
    left alone, the bead top is a perfect-conductor outer boundary that
    shorts each pin to its shell."""

    def _ports(self, plane):
        hfss = bead_model()
        layout = FakeLayout(chip_length=6.3, chip_side=6.3, bonding_clearence=0.6)
        names = ports.place_ports(hfss, layout, {**DRIVEN, "reference_plane": plane})
        return hfss, names

    def test_lumped_and_gap_planes_terminate_both_connectors(self):
        for plane in (ports.REFERENCE_PLANE_LAUNCH, ports.REFERENCE_PLANE_BONDS,
                      ports.REFERENCE_PLANE_CHIP):
            hfss, names = self._ports(plane)
            self.assertEqual([p["name"] for p in hfss.wave_ports],
                             ["connector_load_a", "connector_load_b"], plane)
            self.assertEqual(names, ["port_1", "port_2"], plane)

    def test_terminations_are_matched_to_the_run_impedance(self):
        hfss, _ = self._ports(ports.REFERENCE_PLANE_CHIP)
        self.assertTrue(all(p["impedance"] == 50.0 for p in hfss.wave_ports))

    def test_the_connector_plane_has_no_terminations(self):
        hfss, names = self._ports(ports.REFERENCE_PLANE_CONNECTOR)
        self.assertEqual([p["name"] for p in hfss.wave_ports], ["port_1", "port_2"])

    def test_termination_names_carry_no_number(self):
        """The results reader groups ports by their trailing number; a
        termination must never be mistaken for one of the run's ports."""
        for index in ("1", "2"):
            self.assertFalse(ports._termination_name(index)[-1].isdigit())

    def test_a_connector_that_cannot_be_terminated_is_a_warning_not_a_failure(self):
        hfss = bead_model()
        for name in [n for n in hfss.modeler.object_names if n.endswith("_dielectric")]:
            hfss.modeler.object_names.remove(name)
        messages = []
        names = ports.place_ports(hfss, FakeLayout(), {**DRIVEN, "reference_plane": ports.REFERENCE_PLANE_LAUNCH},
                                  log=messages.append)
        self.assertEqual(names, ["port_1", "port_2"])
        self.assertTrue(any("WARNING" in m and "terminated" in m for m in messages))


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
