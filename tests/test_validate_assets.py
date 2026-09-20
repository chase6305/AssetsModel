import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
import math
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_assets.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("validate_assets", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class ValidationTests(unittest.TestCase):
    def validate(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.urdf"
            path.write_text(f'<robot name="test">{body}</robot>')
            return validator.validate_urdf(path)

    def test_fixed_frame_without_inertia_is_valid(self):
        result = self.validate('<link name="base"/><link name="tip"/>'
                               '<joint name="j" type="fixed"><parent link="base"/><child link="tip"/></joint>')
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_missing_resource_and_disconnected_link(self):
        result = self.validate('<link name="base"><visual><geometry><mesh filename="missing.stl"/></geometry></visual></link><link name="tip"/>')
        self.assertTrue(any("Missing resource" in e for e in result["errors"]))
        self.assertTrue(any("one root" in e for e in result["errors"]))

    def test_bad_coordinate_and_limits(self):
        result = self.validate('<link name="base"/><link name="tip"/><joint name="j" type="revolute">'
                               '<parent link="base"/><child link="tip"/><origin xyz="0 0 0.156.5"/>'
                               '<axis xyz="0 0 0"/><limit lower="1" upper="-1" effort="nan" velocity="1"/></joint>')
        for text in ("invalid xyz", "axis is zero", "lower limit", "invalid effort"):
            self.assertTrue(any(text in e for e in result["errors"]), text)

    def test_disconnected_cycle_with_single_root(self):
        result = self.validate('<link name="base"/><link name="a"/><link name="b"/>'
                               '<joint name="j1" type="fixed"><parent link="a"/><child link="b"/></joint>'
                               '<joint name="j2" type="fixed"><parent link="b"/><child link="a"/></joint>')
        self.assertIn("Link graph contains a cycle", result["errors"])

    def test_mimic_cycle(self):
        result = self.validate('<link name="a"/><link name="b"/><link name="c"/>'
                               '<joint name="j1" type="continuous"><parent link="a"/><child link="b"/><mimic joint="j2"/></joint>'
                               '<joint name="j2" type="continuous"><parent link="b"/><child link="c"/><mimic joint="j1"/></joint>')
        self.assertIn("Mimic graph contains a cycle", result["errors"])

    def test_escaping_and_windows_paths(self):
        for filename in ("../mesh.stl", "C:/mesh.stl", "package://mesh.stl"):
            result = self.validate(f'<link name="base"><visual><geometry><mesh filename="{filename}"/></geometry></visual></link>')
            self.assertTrue(result["errors"])

    def test_missing_inertia_is_reported_without_fabricating_it(self):
        result = self.validate('<link name="base"><visual><geometry><box size="1 1 1"/></geometry></visual></link>')
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["warnings"])

    def test_fr3_fingers_close_and_open_symmetrically(self):
        folder = SCRIPT.parents[1] / "RobotModel" / "Franka" / "FR3"
        for filename in ("FR3Hand.urdf", "FR3WithHand.urdf"):
            robot = ET.parse(folder / filename).getroot()
            first = robot.find("joint[@name='fr3_finger_joint1']")
            second = robot.find("joint[@name='fr3_finger_joint2']")
            self.assertEqual(second.find("mimic").get("joint"), first.get("name"))
            for q in (0.0, 0.02, 0.04):
                positions = []
                for joint in (first, second):
                    y0 = float(joint.find("origin").get("xyz").split()[1])
                    axis_y = float(joint.find("axis").get("xyz").split()[1])
                    yaw = float(joint.find("origin").get("rpy").split()[2])
                    positions.append(y0 + q * axis_y * math.cos(yaw))
                self.assertAlmostEqual(positions[0], -positions[1])
                self.assertAlmostEqual(positions[0] - positions[1], 2 * q)

    def test_fr3_variants_have_inertia_and_matching_component_parameters(self):
        folder = SCRIPT.parents[1] / "RobotModel" / "Franka" / "FR3"
        full = ET.parse(folder / "FR3WithHand.urdf").getroot()
        def signature(element):
            return element.tag, element.attrib, [signature(child) for child in element]
        for filename in ("FR3.urdf", "FR3Hand.urdf", "FR3WithHand.urdf"):
            result = validator.validate_urdf(folder / filename, glb_only=True)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["warnings"], [])
            robot = ET.parse(folder / filename).getroot()
            for element in robot:
                if element.tag not in ("link", "joint"):
                    continue
                counterpart = full.find(f"{element.tag}[@name='{element.get('name')}']")
                self.assertEqual(signature(element), signature(counterpart))
        # FR3 joint 6 differs substantially from the retired Panda limits.
        limit = full.find("joint[@name='fr3_joint6']/limit")
        self.assertEqual((float(limit.get("lower")), float(limit.get("upper"))), (0.5445, 4.5169))
        tcp = full.find("joint[@name='fr3_hand_tcp_joint']/origin")
        self.assertEqual(tcp.get("xyz"), "0 0 0.1034")


if __name__ == "__main__":
    unittest.main()
