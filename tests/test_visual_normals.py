"""Shading refinement must preserve geometry, hard edges and authored smooth groups."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import numpy as np
    import trimesh
    from convert_glb import GLBWriter
    from glb_format import read_glb, validate_glb
    from prepare_preview import accessor_array
    from refine_materials import encode_glb
    from refine_visual_normals import refine
except ImportError:
    np = None


@unittest.skipIf(np is None, 'Optional mesh dependencies are unavailable')
class VisualNormalRefinementTests(unittest.TestCase):
    def flat_part(self, writer, name, mesh, material=None):
        vertices = mesh.vertices[mesh.faces].reshape(-1, 3)
        faces = np.arange(len(vertices)).reshape(-1, 3)
        normals = np.repeat(mesh.face_normals, 3, axis=0)
        writer.add(name, vertices, faces, normals, material)

    def corners(self, tree, binary, mesh_index=0, semantic='POSITION'):
        primitive = tree['meshes'][mesh_index]['primitives'][0]
        faces = accessor_array(tree, binary, primitive['indices']).reshape(-1, 3)
        return accessor_array(tree, binary, primitive['attributes'][semantic])[faces]

    def test_cube_hard_edges_are_not_rounded(self):
        writer = GLBWriter('hard cube')
        self.flat_part(writer, 'cube', trimesh.creation.box())
        tree, binary = read_glb(writer.export())
        target, target_binary, edits = refine(tree, binary)
        self.assertEqual(tree, target)
        self.assertEqual(binary, target_binary)
        self.assertTrue(all(edit['status'] == 'retained' for edit in edits))

    def test_cylinder_sides_smooth_but_caps_and_triangle_positions_stay_exact(self):
        writer = GLBWriter('flat cylinder')
        self.flat_part(writer, 'cylinder', trimesh.creation.cylinder(radius=1, height=2, sections=24))
        tree, binary = read_glb(writer.export())
        target, target_binary, edits = refine(tree, binary)
        np.testing.assert_array_equal(self.corners(tree, binary), self.corners(target, target_binary))
        old = self.corners(tree, binary, semantic='NORMAL')
        new = self.corners(target, target_binary, semantic='NORMAL')
        caps = np.abs(old[:, :, 2]).min(axis=1) > .9
        np.testing.assert_array_equal(old[caps], new[caps])
        self.assertGreater(edits[0]['changed_normal_corners'], 0)
        self.assertTrue(np.any(np.linalg.norm(new[~caps]-new[~caps, :1], axis=2) > .01))
        validate_glb(encode_glb(target, target_binary))

    def test_existing_smooth_groups_materials_transforms_and_aux_lines_survive(self):
        writer = GLBWriter('mixed shading')
        red = {'name': 'red paint', 'pbrMetallicRoughness': {'baseColorFactor': [1, 0, 0, 1]}}
        blue = {'name': 'blue paint', 'pbrMetallicRoughness': {'baseColorFactor': [0, 0, 1, 1]}}
        self.flat_part(writer, 'flat-cylinder', trimesh.creation.cylinder(radius=1, height=2, sections=16), red)
        sphere = trimesh.creation.icosphere(subdivisions=1)
        writer.add('authored-smooth-sphere', sphere.vertices, sphere.faces, sphere.vertex_normals, blue)
        writer.add('guide', np.array([[0, 0, 0], [1, 1, 1]]), np.array([[0, 1]]), mode=1)
        writer.tree['nodes'][1]['translation'] = [3, 2, 1]
        tree, binary = read_glb(writer.export())
        original = copy.deepcopy(tree)
        target, target_binary, edits = refine(tree, binary)
        self.assertEqual(tree, original, 'Input JSON must stay read-only')
        self.assertEqual(tree['materials'], target['materials'])
        self.assertEqual(tree['nodes'], target['nodes'])
        self.assertEqual(tree['scenes'], target['scenes'])
        self.assertEqual(edits[1]['status'], 'retained')
        np.testing.assert_array_equal(self.corners(tree, binary, 1, 'NORMAL'), self.corners(target, target_binary, 1, 'NORMAL'))
        for mi in (0, 1):
            np.testing.assert_array_equal(self.corners(tree, binary, mi), self.corners(target, target_binary, mi))
        primitive = target['meshes'][2]['primitives'][0]
        self.assertEqual(primitive['mode'], 1)
        validate_glb(encode_glb(target, target_binary))

    def test_second_pass_retains_new_authored_smooth_normals(self):
        writer = GLBWriter('cylinder')
        self.flat_part(writer, 'cylinder', trimesh.creation.cylinder(radius=1, height=2, sections=16))
        tree, binary = read_glb(writer.export())
        first, first_bin, _ = refine(tree, binary)
        second, second_bin, edits = refine(first, first_bin)
        self.assertEqual(first, second)
        self.assertEqual(first_bin, second_bin)
        self.assertEqual(edits[0]['status'], 'retained')

    def test_uniformly_slanted_authored_normals_are_retained(self):
        writer = GLBWriter('intentional normal field')
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
        normal = np.array([.2, 0, .98]);normal /= np.linalg.norm(normal)
        writer.add('plane', vertices, np.array([[0, 1, 2]]), np.tile(normal, (3, 1)))
        tree, binary = read_glb(writer.export())
        for preserve in (False, True):
            with self.subTest(preserve_direction_outliers=preserve):
                target, target_binary, edits = refine(tree, binary, preserve_direction_outliers=preserve)
                self.assertEqual(tree, target)
                self.assertEqual(binary, target_binary)
                self.assertEqual(edits[0]['reason'], 'authored normals differ from geometric face directions')

    def test_local_outlier_policy_smooths_eligible_faces_and_preserves_outlier_bytes(self):
        writer = GLBWriter('small authored exception')
        cylinder = trimesh.creation.cylinder(radius=1, height=2, sections=24)
        vertices = cylinder.vertices[cylinder.faces].reshape(-1, 3)
        normals = np.repeat(cylinder.face_normals, 3, axis=0).astype('<f4')
        triangle = np.array([[2, 0, 0], [2.001, 0, 0], [2, .001, 0]])
        outlier = np.array([.2, -0.0, .98], dtype='<f4')
        outlier /= np.linalg.norm(outlier)
        vertices = np.vstack((vertices, triangle))
        normals = np.vstack((normals, np.tile(outlier, (3, 1))))
        writer.add('one primitive', vertices, np.arange(len(vertices)).reshape(-1, 3), normals)
        tree, binary = read_glb(writer.export())
        strict, strict_bin, _ = refine(tree, binary)
        self.assertEqual(tree, strict)
        self.assertEqual(binary, strict_bin)
        target, target_bin, edits = refine(tree, binary, preserve_direction_outliers=True)
        np.testing.assert_array_equal(self.corners(tree, binary), self.corners(target, target_bin))
        before = self.corners(tree, binary, semantic='NORMAL')
        after = self.corners(target, target_bin, semantic='NORMAL')
        self.assertEqual(before[-1].tobytes(), after[-1].tobytes())
        self.assertGreater(np.linalg.norm(before[:-1]-after[:-1]), .1)
        self.assertEqual(edits[0]['protected_faces'], 1)
        self.assertEqual(edits[0]['eligible_faces'], len(cylinder.faces))
        self.assertLess(edits[0]['protected_area_fraction'], 1e-5)
        self.assertTrue(edits[0]['protected_normal_corners_byte_exact'])
        validate_glb(encode_glb(target, target_bin))

    def test_repacking_does_not_silently_discard_buffer_metadata(self):
        writer = GLBWriter('metadata')
        self.flat_part(writer, 'cube', trimesh.creation.box())
        tree, binary = read_glb(writer.export())
        for key in ('buffers', 'bufferViews', 'accessors'):
            modified = copy.deepcopy(tree)
            modified[key][0]['extras'] = {'meaning': 'must survive'}
            with self.assertRaisesRegex(ValueError, 'metadata'):
                refine(modified, binary)

    def test_nonindexed_auxiliary_primitive_is_explicitly_rejected(self):
        writer = GLBWriter('unindexed')
        self.flat_part(writer, 'cube', trimesh.creation.box())
        tree, binary = read_glb(writer.export())
        tree['meshes'][0]['primitives'][0].pop('indices')
        with self.assertRaisesRegex(ValueError, 'Non-indexed'):
            refine(tree, binary)

    def test_selection_retains_nonselected_flat_primitive_attributes_byte_exact(self):
        writer = GLBWriter('explicit selected scope')
        cylinder = trimesh.creation.cylinder(radius=1, height=2, sections=24)
        self.flat_part(writer, 'selected-cylinder', cylinder)
        self.flat_part(writer, 'protected-cylinder', cylinder)
        writer.tree['nodes'][1]['translation'] = [3, 0, 0]
        tree, binary = read_glb(writer.export())
        target, target_binary, edits = refine(tree, binary, eligible_primitives={(0, 0)})
        self.assertEqual(edits[0]['status'], 'refined')
        self.assertEqual(edits[1]['reason'], 'outside selected primitive scope')
        for semantic in ('POSITION', 'NORMAL'):
            before = self.corners(tree, binary, 1, semantic)
            after = self.corners(target, target_binary, 1, semantic)
            self.assertEqual(before.tobytes(), after.tobytes())
        np.testing.assert_array_equal(self.corners(tree, binary, 0), self.corners(target, target_binary, 0))
        self.assertGreater(np.linalg.norm(self.corners(tree, binary, 0, 'NORMAL')-self.corners(target, target_binary, 0, 'NORMAL')), .1)
        self.assertEqual(tree['nodes'], target['nodes'])
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            refine(tree, binary, eligible_primitives={(99, 0)})


if __name__ == '__main__':
    unittest.main()
