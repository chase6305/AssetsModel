"""Appearance changes must not modify stored geometry or existing materials."""
import sys
import json
import struct
import subprocess
import tempfile
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from embed_urdf_materials import embed, references
from glb_format import read_glb


class EmbedMaterialsTests(unittest.TestCase):
    def setUp(self):
        # Self-contained fixture: changing a production asset's material must
        # not change what these tests exercise.
        binary = struct.pack("<9f3H", 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 2)
        tree = {"asset": {"version": "2.0"}, "scene": 0,
                "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
                "buffers": [{"byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                                {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3,
                               "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
                              {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}]}
        raw = json.dumps(tree).encode()
        raw += b" " * (-len(raw) % 4)
        binary += b"\0" * (-len(binary) % 4)
        self.data = (struct.pack("<III", 0x46546C67, 2, 28 + len(raw) + len(binary))
                     + struct.pack("<II", len(raw), 0x4E4F534A) + raw
                     + struct.pack("<II", len(binary), 0x004E4942) + binary)
        self.color = (0.15, 0.25, 0.35, 1.0)

    def test_preserves_binary_and_all_geometry_metadata(self):
        before, binary = read_glb(self.data)
        payload, _ = embed(self.data, {self.color})
        after, result_binary = read_glb(payload)
        self.assertEqual(binary, result_binary)
        for key in ("nodes", "scenes", "accessors", "bufferViews", "buffers"):
            self.assertEqual(before.get(key), after.get(key))
        self.assertEqual(after["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"], list(self.color))
        self.assertEqual(after["materials"][0]["pbrMetallicRoughness"]["metallicFactor"], 0)
        self.assertIsNone(embed(payload, {(1, 0, 0, 1)})[0])

    def test_ambiguous_and_missing_colors_are_retained(self):
        for colors in ({None}, {None, self.color}, {self.color, (1, 0, 0, 1)}, set()):
            self.assertIsNone(embed(self.data, colors)[0])

    def test_rejects_nonfinite_and_out_of_range_color(self):
        for color in ((float("nan"), 0, 0, 1), (2, 0, 0, 1), (0, 0, 0)):
            with self.assertRaises(ValueError):
                embed(self.data, {color})


class MaterialReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "assets"
        self.model = self.root / "RobotModel" / "sample"
        self.model.mkdir(parents=True)
        self.target = self.model / "part.glb"
        self.color = (0.1, 0.2, 0.3, 1.0)
        self.urdf = '<robot name="sample"><material name="paint"><color rgba="0.1 0.2 0.3 1"/></material><link name="base"><visual><geometry><mesh filename="part.glb"/></geometry><material name="paint"/></visual></link></robot>'

    def test_named_colors_and_shared_collision_paths_are_resolved(self):
        collision = '<collision><geometry><mesh filename="./part.glb"/></geometry></collision>'
        (self.model / "a.urdf").write_text(self.urdf.replace("</link>", collision + "</link>"))
        colors, roles = references(self.root)
        self.assertEqual(colors[self.target], {self.color})
        self.assertEqual(roles[self.target], {"visual", "collision"})

    def test_cross_urdf_color_conflicts_are_retained(self):
        (self.model / "a.urdf").write_text(self.urdf)
        (self.model / "b.urdf").write_text(self.urdf.replace("0.1 0.2 0.3 1", "1 0 0 1"))
        colors, _ = references(self.root)
        self.assertEqual(colors[self.target], {self.color, (1.0, 0.0, 0.0, 1.0)})

    def test_one_missing_instance_color_is_recorded(self):
        (self.model / "a.urdf").write_text(self.urdf)
        (self.model / "b.urdf").write_text(self.urdf.replace('<material name="paint"/>', ''))
        colors, _ = references(self.root)
        self.assertEqual(colors[self.target], {self.color, None})

    def test_escaping_resource_paths_are_rejected(self):
        (self.model / "a.urdf").write_text(self.urdf.replace("part.glb", "../../../outside.glb"))
        with self.assertRaisesRegex(ValueError, "outside asset root"):
            references(self.root)

    def test_cli_retains_a_visual_glb_shared_with_collision(self):
        fixture = EmbedMaterialsTests()
        fixture.setUp()
        self.target.write_bytes(fixture.data)
        collision = '<collision><geometry><mesh filename="part.glb"/></geometry></collision>'
        (self.model / "a.urdf").write_text(self.urdf.replace("</link>", collision + "</link>"))
        script = Path(__file__).resolve().parents[1] / "scripts" / "embed_urdf_materials.py"
        result = subprocess.run([sys.executable, str(script), "--root", str(self.root),
                                 "--apply", "--backup-dir", str(Path(self.temporary.name) / "backup"),
                                 "--json", str(self.root / "report.json")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root / "report.json").read_text())
        self.assertEqual(report["summary"]["changed"], 0)
        self.assertIn("shared with collision", report["files"][0]["reason"])
        self.assertEqual(self.target.read_bytes(), fixture.data)


if __name__ == "__main__":
    unittest.main()
