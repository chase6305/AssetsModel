"""Check that review renders retain the asset's material and surface semantics."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

AVAILABLE = all(importlib.util.find_spec(x) for x in ("numpy", "trimesh", "collada", "scipy"))
if AVAILABLE:
    import numpy as np
    from convert_glb import GLBWriter
    from prepare_preview import load_parts, write_pack


@unittest.skipUnless(AVAILABLE, "Optional geometry dependencies not installed")
class PreviewTests(unittest.TestCase):
    def make_writer(self, material=None):
        writer = GLBWriter("test.dae")
        writer.add("surface", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]],
                   [[0, 0, 1]] * 3, material)
        return writer

    def load(self, writer, transform=None, color=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "surface.glb"
            path.write_bytes(writer.export())
            return list(load_parts(path, np.eye(4) if transform is None else transform,
                                   [0.6, 0.65, 0.7, 1] if color is None else color))

    def test_preserves_floating_point_pbr_instead_of_flattening_to_urdf_color(self):
        material = {"pbrMetallicRoughness": {"baseColorFactor": [0.0123456789, 0.23456789, 0.9, 0.6],
                                             "metallicFactor": 0.82, "roughnessFactor": 0.27},
                    "emissiveFactor": [0.1, 0.2, 0.3], "doubleSided": True, "alphaMode": "MASK", "alphaCutoff": 0.4}
        part, = self.load(self.make_writer(material), color=[1, 0, 0, 1])
        properties = part["material"]
        self.assertEqual(properties["base_color"], [0.0123456789, 0.23456789, 0.9, 0.6])
        self.assertEqual((properties["metallic"], properties["roughness"]), (0.82, 0.27))
        self.assertEqual(properties["emissive"], [0.1, 0.2, 0.3])
        self.assertEqual((properties["alpha_mode"], properties["alpha_cutoff"], properties["double_sided"]),
                         ("MASK", 0.4, True))

    def test_unpainted_mesh_uses_urdf_color_without_inventing_metal(self):
        part, = self.load(self.make_writer(), color=[0.2, 0.3, 0.4, 1])
        self.assertEqual(part["material"]["base_color"], [0.2, 0.3, 0.4, 1])
        self.assertEqual(part["material"]["metallic"], 0)
        self.assertEqual(part["material"]["roughness"], 1)

    def test_embedded_material_uses_gltf_defaults_for_omitted_pbr_fields(self):
        part, = self.load(self.make_writer({"name": "default"}))
        self.assertEqual(part["material"]["base_color"], [1, 1, 1, 1])
        self.assertEqual(part["material"]["metallic"], 1)
        self.assertEqual(part["material"]["roughness"], 1)

    def test_mirrored_scale_keeps_winding_consistent_with_normals(self):
        matrix = np.diag([-2.0, 3.0, 4.0, 1.0])
        matrix[:3, 3] = [5, 6, 7]
        part, = self.load(self.make_writer(), matrix)
        triangles = part["vertices"][part["faces"]]
        np.testing.assert_array_equal(triangles, [[[5, 9, 7], [3, 6, 7], [5, 6, 7]]])
        normal = np.cross(triangles[0, 1] - triangles[0, 0], triangles[0, 2] - triangles[0, 0])
        normal /= np.linalg.norm(normal)
        np.testing.assert_allclose(part["normals"], [normal] * 3)

    def test_default_scene_excludes_auxiliary_guides(self):
        writer = self.make_writer()
        writer.add("guide", [[5, 5, 5], [6, 6, 6]], [[0, 1]], mode=1)
        self.assertEqual(len(self.load(writer)), 1)

    def test_scene_hierarchy_and_column_major_node_matrix_are_applied(self):
        writer = self.make_writer()
        # Child scale, then parent translation: glTF matrices are column-major.
        writer.tree["nodes"][0]["scale"] = [2, 3, 4]
        writer.tree["nodes"].append({"children": [0], "matrix": [1, 0, 0, 0,
                                     0, 1, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1]})
        writer.tree["scenes"][0]["nodes"] = [1]
        part, = self.load(writer)
        np.testing.assert_allclose(part["vertices"], [[5, 6, 7], [7, 6, 7], [5, 9, 7]])
        np.testing.assert_allclose(part["normals"], [[0, 0, 1]] * 3)

    def test_embedded_texture_bytes_and_uv_survive_packing(self):
        # The packer copies image bytes rather than resampling a texture into a
        # flat main_color. Blender decodes the image when rendering the pack.
        image_data = b"image data passed through unchanged"
        writer = self.make_writer({"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}})
        primitive = writer.tree["meshes"][0]["primitives"][0]
        primitive["attributes"]["TEXCOORD_0"] = writer.accessor(
            np.asarray([[0, 0], [1, 0], [0, 1]], dtype="<f4"), 5126, "VEC2", 34962)
        writer.tree["images"] = [{"bufferView": len(writer.tree["bufferViews"]), "mimeType": "image/png"}]
        writer.tree["bufferViews"].append({"buffer": 0, "byteOffset": len(writer.binary), "byteLength": len(image_data)})
        writer.binary.extend(image_data)
        writer.tree["textures"] = [{"source": 0}]
        part, = self.load(writer)
        np.testing.assert_array_equal(part["uv"], [[0, 1], [1, 1], [0, 0]])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_pack([part], output)
            metadata, = json.loads((output / "scene.json").read_text())
            texture = metadata["material"]["base_color_texture"]
            self.assertEqual((output / texture["file"]).read_bytes(), image_data)
            with np.load(output / metadata["file"]) as data:
                np.testing.assert_array_equal(data["uv"], [[0, 1], [1, 1], [0, 0]])

    def test_unsupported_material_is_rejected_instead_of_silently_misrepresented(self):
        writer = self.make_writer({"normalTexture": {"index": 0}})
        with self.assertRaisesRegex(ValueError, "Unsupported preview material"):
            self.load(writer)


if __name__ == "__main__":
    unittest.main()
