"""Preview regressions: exact linear PBR, authored normals, and actual URDF motion."""
import importlib.util
import json
import math
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
AVAILABLE = all(importlib.util.find_spec(name) for name in ("numpy", "trimesh", "collada", "scipy"))
if AVAILABLE:
    import numpy as np
    from convert_glb import GLBWriter
    from prepare_preview import joint_frames, load_parts, prepare


@unittest.skipUnless(AVAILABLE, "Optional geometry dependencies not installed")
class PreviewTests(unittest.TestCase):
    def fixture(self, path):
        writer = GLBWriter("test")
        material = {"name": "paint", "pbrMetallicRoughness": {"baseColorFactor": [0.2123456789, 0.0456789123, 0.0043219876, 1],
                    "metallicFactor": 0.19, "roughnessFactor": 0.47}, "doubleSided": True}
        # Authored corner normal intentionally differs from the face normal.
        normal = np.array([0.2, 0.3, 1.0]); normal /= np.linalg.norm(normal)
        writer.add("painted", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]], [normal] * 3, material)
        writer.add("CAD guide", [[0, 0, 0], [9, 9, 9]], [[0, 1]], mode=1)
        path.write_bytes(writer.export())
        return material, normal

    def test_exact_pbr_and_authored_normals_survive_preview_pack(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            material, normal = self.fixture(root / "part.glb")
            parts = prepare(root / "part.glb", root / "pack")
            self.assertEqual(len(parts), 1)  # optional guide scene is excluded
            self.assertEqual(parts[0]["color"], material["pbrMetallicRoughness"]["baseColorFactor"])
            self.assertEqual(parts[0]["metallic"], 0.19)
            self.assertEqual(parts[0]["roughness"], 0.47)
            with np.load(root / "pack/part-0.npz") as packed:
                np.testing.assert_allclose(packed["normals"], [normal] * 3, atol=2e-8)
                np.testing.assert_array_equal(packed["vertices"][packed["faces"]], [[[0, 0, 0], [1, 0, 0], [0, 1, 0]]])

    def test_nonuniform_mirror_preserves_outward_winding_and_normal_transform(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "part.glb"
            _, normal = self.fixture(path)
            scale = np.diag([-2.0, 3.0, 4.0, 1.0])
            part, = list(load_parts(path, scale, [1, 1, 1, 1]))
            expected = normal / [-2, 3, 4]; expected /= np.linalg.norm(expected)
            np.testing.assert_allclose(part["normals"], [expected] * 3, atol=2e-8)
            corners = part["vertices"][part["faces"]][0]
            self.assertGreater(np.dot(np.cross(corners[1]-corners[0], corners[2]-corners[0]), expected), 0)

    def test_default_gltf_material_and_explicit_urdf_color_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'part.glb'
            writer = GLBWriter('no-material')
            writer.add('surface', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
            path.write_bytes(writer.export())
            standalone, = list(load_parts(path, np.eye(4), None))
            self.assertEqual(standalone['material']['color'], [1, 1, 1, 1])
            self.assertEqual(standalone['material']['metallic'], 1)
            self.assertEqual(standalone['material']['roughness'], 1)
            self.assertFalse(standalone['material']['double_sided'])
            urdf_color, = list(load_parts(path, np.eye(4), [0.1, 0.2, 0.3, 1]))
            self.assertEqual(urdf_color['material']['color'], [0.1, 0.2, 0.3, 1])

    def test_nested_gltf_trs_preserves_model_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'part.glb'
            writer = GLBWriter('nested')
            writer.add('surface', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
            writer.tree['nodes'][0].update(scale=[2, 3, 4])
            writer.tree['nodes'].append({'translation': [10, 20, 30], 'rotation': [0, 0, math.sqrt(0.5), math.sqrt(0.5)], 'children': [0]})
            writer.tree['scenes'][0]['nodes'] = [1]
            path.write_bytes(writer.export())
            part, = list(load_parts(path, np.eye(4), None))
            np.testing.assert_allclose(part['vertices'], [[10, 20, 30], [10, 22, 30], [7, 20, 30]], atol=1e-13)

    def test_urdf_primitive_shapes_keep_dimensions_and_surface_normals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'primitives.urdf'
            path.write_text('''<robot name="primitives"><link name="base">
            <visual><origin xyz="1 2 3" rpy="0 0 1.5707963267948966"/><geometry><box size="2 4 6"/></geometry></visual>
            <visual><geometry><cylinder radius="2" length="6"/></geometry></visual>
            <visual><geometry><sphere radius="2"/></geometry></visual>
            </link></robot>''')
            parts = prepare(path, root / 'pack')
            self.assertEqual(len(parts), 3)
            with np.load(root / 'pack/part-0.npz') as box:
                np.testing.assert_allclose(box['vertices'].min(axis=0), [-1, 1, 0], atol=1e-14)
                np.testing.assert_allclose(box['vertices'].max(axis=0), [3, 3, 6], atol=1e-14)
            with np.load(root / 'pack/part-1.npz') as cylinder:
                np.testing.assert_allclose(cylinder['vertices'].min(axis=0), [-2, -2, -3], atol=1e-14)
                np.testing.assert_allclose(cylinder['vertices'].max(axis=0), [2, 2, 3], atol=1e-14)
                normals = cylinder['normals']
                side = np.abs(normals[:, 2]) < 0.5
                np.testing.assert_allclose(normals[side, 2], 0, atol=1e-14)
                np.testing.assert_allclose(np.linalg.norm(normals, axis=1), 1, atol=1e-14)
            with np.load(root / 'pack/part-2.npz') as sphere:
                np.testing.assert_allclose(np.linalg.norm(sphere['vertices'], axis=1), 2, atol=1e-14)
                expected = sphere['vertices'][sphere['faces']].reshape(-1, 3) / 2
                np.testing.assert_allclose(sphere['normals'], expected, atol=1e-14)

    def test_rotation_translation_and_chained_mimic_are_applied_in_joint_frame(self):
        robot = ET.fromstring('''<robot name="test"><link name="base"/><link name="arm"/><link name="left"/><link name="right"/><link name="tip"/>
        <joint name="turn" type="revolute"><parent link="base"/><child link="arm"/><origin xyz="1 0 0"/><axis xyz="0 0 1"/><limit lower="-2" upper="2"/></joint>
        <joint name="slide" type="prismatic"><parent link="arm"/><child link="left"/><axis xyz="1 0 0"/><limit lower="0" upper="1"/></joint>
        <joint name="mirror" type="prismatic"><parent link="arm"/><child link="right"/><axis xyz="1 0 0"/><limit lower="-1" upper="0"/><mimic joint="slide" multiplier="-1"/></joint>
        <joint name="chain" type="prismatic"><parent link="arm"/><child link="tip"/><axis xyz="0 1 0"/><limit lower="0" upper="1"/><mimic joint="mirror" multiplier="-2" offset="0.1"/></joint></robot>''')
        frames, values = joint_frames(robot, {"turn": math.pi/2, "slide": 0.2})
        np.testing.assert_allclose(frames["left"][:3, 3], [1, 0.2, 0], atol=1e-14)
        np.testing.assert_allclose(frames["right"][:3, 3], [1, -0.2, 0], atol=1e-14)
        np.testing.assert_allclose(frames["tip"][:3, 3], [0.5, 0, 0], atol=1e-14)
        self.assertEqual(values["chain"], 0.5)
        for bad in ({"missing": 1}, {"slide": 2}, {"mirror": 0.2}, {"slide": float('nan')}):
            with self.assertRaises(ValueError):
                joint_frames(robot, bad)

    def test_fr3_nonzero_pose_matches_yourdfpy_all_link_frames(self):
        if not importlib.util.find_spec("yourdfpy"):
            self.skipTest("yourdfpy not installed")
        import yourdfpy
        path = Path(__file__).resolve().parents[1] / "RobotModel/Franka/FR3/FR3WithHand.urdf"
        pose = {"fr3_joint2": -0.5, "fr3_joint4": -1.8, "fr3_joint6": 1.4,
                "fr3_joint7": 0.7, "fr3_finger_joint1": 0.03}
        frames, values = joint_frames(ET.parse(path).getroot(), pose)
        robot = yourdfpy.URDF.load(str(path), load_meshes=False, build_scene_graph=True)
        robot.update_cfg(pose)
        for name, matrix in frames.items():
            np.testing.assert_allclose(matrix, robot.scene.graph.get(name)[0], atol=1e-12, rtol=0)
        self.assertAlmostEqual(values['fr3_finger_joint2'], 0.03)
        with self.assertRaisesRegex(ValueError, 'outside'):
            joint_frames(ET.parse(path).getroot(), {})


if __name__ == '__main__':
    unittest.main()
