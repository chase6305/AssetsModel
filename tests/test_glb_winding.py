import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
AVAILABLE = all(importlib.util.find_spec(x) for x in ("numpy", "trimesh", "collada", "scipy"))
if AVAILABLE:
    import numpy as np
    import trimesh
    from convert_glb import GLBWriter
    from glb_format import read_glb
    from audit_asset_quality import array
    from repair_glb_winding import repair
    from batch_glb import export


@unittest.skipUnless(AVAILABLE, "Optional geometry dependencies not installed")
class WindingRepairTests(unittest.TestCase):
    def fixture(self, reverse=False, duplicate=False, remove=False, normals=True):
        mesh = trimesh.creation.icosphere(subdivisions=1)
        # Expanded corners ensure topology is found across hard seams too.
        vertices = mesh.vertices[mesh.faces].reshape(-1, 3)
        faces = np.arange(len(vertices)).reshape(-1, 3)
        normal_values = np.repeat(mesh.face_normals, 3, axis=0)
        if reverse:
            faces[0] = faces[0, [0, 2, 1]]
        if duplicate:
            faces = np.vstack((faces, faces[0]))
        if remove:
            pair = mesh.face_adjacency[(mesh.face_adjacency == 0).any(axis=1)][0]
            neighbor = int(pair[pair != 0][0])
            faces = np.delete(faces, neighbor, axis=0)
        writer = GLBWriter("fixture")
        writer.add("surface", vertices, faces, normal_values if normals else None,
                   {"pbrMetallicRoughness": {"baseColorFactor": [0.3, 0.4, 0.5, 1]}})
        return writer.export()

    def test_isolated_reverse_repairs_only_index_bytes_and_is_idempotent(self):
        before = self.fixture(reverse=True)
        after, evidence = repair(before)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["triangle"], 0)
        self.assertEqual(len(set(evidence[0]["neighbor_triangles"])), 3)
        tree_before, binary_before = read_glb(before)
        tree_after, binary_after = read_glb(after)
        self.assertEqual(tree_before, tree_after)
        for semantic in ("POSITION", "NORMAL"):
            accessor = tree_before["meshes"][0]["primitives"][0]["attributes"][semantic]
            np.testing.assert_array_equal(array(tree_before, binary_before, accessor),
                                          array(tree_after, binary_after, accessor))
        self.assertLessEqual(sum(x != y for x, y in zip(before, after)), 2)
        self.assertEqual(repair(after), (after, []))

    def test_correct_faces_are_unchanged(self):
        before = self.fixture()
        self.assertEqual(repair(before), (before, []))

    def test_duplicate_faces_are_not_repaired(self):
        before = self.fixture(reverse=True, duplicate=True)
        self.assertEqual(repair(before), (before, []))

    def test_open_boundaries_are_not_filled_or_reversed(self):
        before = self.fixture(reverse=True, remove=True)
        self.assertEqual(repair(before), (before, []))

    def test_missing_normals_are_not_guessed(self):
        before = self.fixture(reverse=True, normals=False)
        self.assertEqual(repair(before), (before, []))

    def test_cross_primitive_coincident_surface_is_not_repaired(self):
        tree, binary = read_glb(self.fixture(reverse=True))
        primitive = tree["meshes"][0]["primitives"][0]
        vertices = array(tree, binary, primitive["attributes"]["POSITION"])
        normals = array(tree, binary, primitive["attributes"]["NORMAL"])
        faces = array(tree, binary, primitive["indices"]).ravel().reshape(-1, 3)
        writer = GLBWriter("coincident.stl")
        for name in ("front", "overlay"):
            writer.add(name, vertices, faces, normals, {"name": name})
        before = writer.export()
        self.assertEqual(repair(before), (before, []))

    def test_transformed_node_is_rejected(self):
        tree, binary = read_glb(self.fixture(reverse=True))
        tree["nodes"][0]["translation"] = [1, 0, 0]
        with self.assertRaisesRegex(ValueError, "identity-node"):
            repair(export(tree, binary))


if __name__ == "__main__":
    unittest.main()
