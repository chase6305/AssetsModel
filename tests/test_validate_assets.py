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

    def test_panda_fingers_close_and_open_symmetrically(self):
        folder = SCRIPT.parents[1] / "RobotModel" / "Franka" / "Panda"
        for filename in ("PandaHand.urdf", "PandaWithHand.urdf"):
            robot = ET.parse(folder / filename).getroot()
            first = robot.find("joint[@name='finger_joint1']")
            second = robot.find("joint[@name='finger_joint2']")
            for q in (0.0, 0.02, 0.04):
                positions = []
                for joint in (first, second):
                    y0 = float(joint.find("origin").get("xyz").split()[1])
                    axis_y = float(joint.find("axis").get("xyz").split()[1])
                    positions.append(y0 + q * axis_y)
                self.assertAlmostEqual(positions[0], -positions[1])
                self.assertAlmostEqual(positions[0] - positions[1], 2 * q)
            right = robot.find("link[@name='rightfinger']")
            for kind in ("visual", "collision"):
                yaw = float(right.find(f"{kind}/origin").get("rpy").split()[2])
                self.assertAlmostEqual(yaw, math.pi, places=6)


if __name__ == "__main__":
    unittest.main()
