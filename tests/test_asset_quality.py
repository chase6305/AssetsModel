import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

AVAILABLE = importlib.util.find_spec("numpy") is not None
if AVAILABLE:
    from audit_asset_quality import audit_glb, audit_repository, scene_meshes


def glb(normals=True, auxiliary=False, material=None):
    """Minimal independent GLB fixture; no conversion or optimization helpers."""
    data = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": 36}]
    accessors = [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}]
    attributes = {"POSITION": 0}
    if normals is not False:
        normal = (0, 0, 1) if normals is True else normals
        views.append({"buffer": 0, "byteOffset": len(data), "byteLength": 36})
        data += struct.pack("<9f", *(normal * 3))
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126, "count": 3, "type": "VEC3"})
        attributes["NORMAL"] = len(accessors) - 1
    views.append({"buffer": 0, "byteOffset": len(data), "byteLength": 6})
    data += struct.pack("<3H", 0, 1, 2)
    accessors.append({"bufferView": len(views) - 1, "componentType": 5123, "count": 3, "type": "SCALAR"})
    primitive = {"attributes": attributes, "indices": len(accessors) - 1}
    tree = {"asset": {"version": "2.0"}, "scene": 0,
            "scenes": [{"nodes": [0, 1]}], "nodes": [{"mesh": 0}, {"mesh": 0}],
            "meshes": [{"primitives": [primitive]}], "bufferViews": views,
            "accessors": accessors, "buffers": [{"byteLength": len(data)}]}
    if material is not None:
        tree["materials"] = [material]
        primitive["material"] = 0
    if auxiliary:
        tree["nodes"].append({"mesh": 0})
        tree["scenes"].append({"nodes": [0, 1, 2]})
    raw = json.dumps(tree).encode()
    raw += b" " * (-len(raw) % 4)
    data += b"\0" * (-len(data) % 4)
    return (struct.pack("<III", 0x46546C67, 2, 28 + len(raw) + len(data))
            + struct.pack("<II", len(raw), 0x4E4F534A) + raw
            + struct.pack("<II", len(data), 0x004E4942) + data)


@unittest.skipUnless(AVAILABLE, "Optional NumPy dependency not installed")
class AssetQualityTests(unittest.TestCase):
    def audit(self, payload, roles=("visual",)):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.glb"
            path.write_bytes(payload)
            return audit_glb(path, roles)

    def test_zero_nonfinite_and_nonunit_normals_fail(self):
        for normal in [(0, 0, 0), (0, 0, float("nan")), (0, 0, 2)]:
            with self.subTest(normal=normal):
                result = self.audit(glb(normals=normal))
                self.assertEqual(result["invalid_normal_vertices"], 3)
                self.assertTrue(result["errors"])
        self.assertFalse(self.audit(glb())["errors"])

    def test_missing_visual_normals_warn_but_collision_normals_are_optional(self):
        self.assertEqual(self.audit(glb(normals=False))["missing_visual_normal_primitives"], 1)
        collision = self.audit(glb(normals=False), roles=("collision",))
        self.assertEqual(collision["missing_visual_normal_primitives"], 0)
        self.assertFalse(collision["errors"] + collision["warnings"])

    def test_runtime_counts_instances_only_in_active_scene(self):
        result = self.audit(glb(auxiliary=True))
        self.assertEqual(result["stored_triangles"], 1)
        self.assertEqual(result["default_scene_triangles"], 2)
        self.assertEqual(result["default_scene_primitives"], 2)

    def test_invalid_material_factor_fails_and_opaque_alpha_warns(self):
        result = self.audit(glb(material={"pbrMetallicRoughness": {"roughnessFactor": -0.1}}))
        self.assertTrue(any("roughnessFactor" in x for x in result["errors"]))
        result = self.audit(glb(material={"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 0.5]}}))
        self.assertTrue(any("alpha" in x for x in result["warnings"]))
        self.assertFalse(self.audit(glb(material={}))["errors"])

    def test_scene_cycles_fail(self):
        with self.assertRaisesRegex(ValueError, "Cycle"):
            list(scene_meshes({"scenes": [{"nodes": [0]}], "nodes": [{"children": [0]}]}))

    def test_urdf_repeated_references_count_draws_but_not_transfer_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "RobotModel" / "Example"
            folder.mkdir(parents=True)
            mesh = folder / "part.glb"
            mesh.write_bytes(glb())
            (folder / "example.urdf").write_text('''<robot name="example"><link name="base">
<visual><geometry><mesh filename="part.glb"/></geometry></visual>
<visual><geometry><mesh filename="part.glb"/></geometry></visual>
<collision><geometry><mesh filename="part.glb"/></geometry></collision>
</link></robot>''')
            report = audit_repository(root)
            model = report["models"][0]
            self.assertEqual(model["visual_triangles"], 4)
            self.assertEqual(model["collision_triangles"], 2)
            self.assertEqual(model["visual_unique_bytes"], mesh.stat().st_size)
            self.assertFalse(report["summary"]["errors"])


if __name__ == "__main__":
    unittest.main()
