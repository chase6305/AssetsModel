import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from glb_format import read_glb, validate_glb

AVAILABLE = all(importlib.util.find_spec(x) for x in ("numpy", "trimesh", "collada", "scipy"))
if AVAILABLE:
    import numpy as np
    import trimesh
    from convert_glb import GLBWriter, compact, convert, convert_files, smooth_stl_normals


class ContainerTests(unittest.TestCase):
    def test_truncated_glb_fails(self):
        with self.assertRaisesRegex(ValueError, "Truncated"):
            read_glb(b"glTF")


@unittest.skipUnless(AVAILABLE, "Optional geometry dependencies not installed")
class ConversionTests(unittest.TestCase):
    def test_hard_cube_edges_survive_index_compaction(self):
        cube = trimesh.creation.box()
        normals = smooth_stl_normals(cube.vertices, cube.faces)
        vertices, faces, packed_normals = compact(cube.vertices, cube.faces, normals)
        self.assertEqual(len(vertices), 24)
        np.testing.assert_array_equal(vertices[faces], cube.triangles)
        np.testing.assert_allclose(np.linalg.norm(packed_normals, axis=1), 1)

    def test_smooth_sphere_shares_vertices_without_moving_faces(self):
        mesh = trimesh.creation.icosphere(subdivisions=2)
        vertices, faces, normals = compact(mesh.vertices, mesh.faces, smooth_stl_normals(mesh.vertices, mesh.faces))
        self.assertEqual(len(vertices), len(mesh.vertices))
        np.testing.assert_allclose(vertices[faces], mesh.triangles, atol=1e-7)

    def test_auxiliary_lines_are_preserved_outside_default_scene(self):
        writer = GLBWriter("part.dae")
        writer.add("surface", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
        writer.add("guide", [[0, 0, 0], [1, 1, 1]], [[0, 1]], mode=1)
        payload = writer.export()
        self.assertEqual(validate_glb(payload)["lines"], 1)
        scene = trimesh.load_scene(io.BytesIO(payload), file_type="glb", process=False)
        self.assertEqual(len(scene.graph.nodes_geometry), 1)
        self.assertIsInstance(scene.geometry[scene.graph[scene.graph.nodes_geometry[0]][1]], trimesh.Trimesh)

    def test_index_width_does_not_wrap_at_65535(self):
        for maximum, component in [(65534, 5123), (65535, 5125)]:
            writer = GLBWriter("part.stl")
            vertices = np.zeros((maximum + 1, 3), dtype=np.float32)
            primitive = writer.add("surface", vertices, [[0, 1, maximum]])
            tree, _ = read_glb(writer.export())
            self.assertEqual(tree["accessors"][primitive["indices"]]["componentType"], component)

    def test_invalid_index_is_rejected(self):
        writer = GLBWriter("part.stl")
        writer.add("surface", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 4]])
        with self.assertRaisesRegex(ValueError, "missing vertex"):
            validate_glb(writer.export())

    def test_collision_export_omits_normals_and_preserves_triangles(self):
        mesh = trimesh.creation.box()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "box.stl"
            mesh.export(path)
            payload, stats = convert(path, collision=True)
        tree, _ = read_glb(payload)
        self.assertNotIn("NORMAL", tree["meshes"][0]["primitives"][0]["attributes"])
        self.assertNotIn("materials", tree)
        self.assertEqual(stats["faces"], 12)
        self.assertEqual(stats["vertices_after"], 8)
        self.assertEqual(stats["max_corner_error_m"], 0)

    def test_import_does_not_overwrite_an_existing_glb_with_different_content(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "part.stl"
            trimesh.creation.box().export(source)
            output = folder / "glb"
            output.mkdir()
            target = output / "part.glb"
            original = b"existing asset content"
            target.write_bytes(original)
            with self.assertRaisesRegex(ValueError, "Existing GLB differs"):
                convert_files([source], output)
            self.assertEqual(target.read_bytes(), original)
            # A later invalid input must not partially replace earlier assets.
            with self.assertRaisesRegex(ValueError, "existing DAE/STL"):
                convert_files([source, folder / "missing.dae"], output, overwrite=True)
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
