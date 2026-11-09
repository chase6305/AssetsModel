"""Independent format and preservation checks for normal-only visual edits."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
AVAILABLE = all(importlib.util.find_spec(name) for name in ('numpy', 'trimesh', 'collada', 'scipy'))
if AVAILABLE:
    import numpy as np
    import trimesh
    from convert_glb import GLBWriter
    from glb_format import read_glb
    from refine_materials import encode_glb
    from refine_visual_normals import refine

from verify_normal_updates import (independent_read_glb, independent_accessor,
                                   independent_corner_rows, verify_normal_update)

# Keep the earlier import name usable for historical one-off audit commands.
independent_compare_normal_update = verify_normal_update


@unittest.skipUnless(AVAILABLE, 'Optional mesh dependencies not installed')
class IndependentNormalPipelineTests(unittest.TestCase):
    def cylinder(self, writer):
        cylinder = trimesh.creation.cylinder(radius=1, height=2, sections=24)
        vertices = cylinder.vertices[cylinder.faces].reshape(-1, 3)
        normals = np.repeat(cylinder.face_normals, 3, axis=0)
        primitive = writer.add('flat cylinder', vertices, np.arange(len(vertices)).reshape(-1, 3), normals)
        return vertices, normals, primitive

    def test_static_morph_targets_are_rejected_before_accessor_repacking(self):
        writer = GLBWriter('static morph source')
        vertices, _normals, primitive = self.cylinder(writer)
        target = writer.accessor(np.tile(np.array([0, 0, .1], dtype='<f4'), (len(vertices), 1)), 5126, 'VEC3', 34962)
        primitive['targets'] = [{'POSITION': target}]
        writer.tree['meshes'][0]['weights'] = [1]
        tree, binary = read_glb(writer.export())
        with self.assertRaisesRegex(ValueError, '[Mm]orph|target|weight'):
            refine(tree, binary)

    def test_retained_rgb_byte_vertices_remain_aligned_or_are_explicitly_rejected(self):
        writer = GLBWriter('mixed retained color')
        self.cylinder(writer)
        primitive = writer.add('colored triangle', [[3, 0, 0], [4, 0, 0], [3, 1, 0]], [[0, 1, 2]], [[0, 0, 1]] * 3)
        color = writer.accessor(np.array([[255, 0, 0, 0], [0, 255, 0, 0], [0, 0, 255, 0]], dtype=np.uint8), 5121, 'VEC4', 34962)
        writer.tree['accessors'][color].update(type='VEC3', normalized=True)
        writer.tree['bufferViews'][writer.tree['accessors'][color]['bufferView']]['byteStride'] = 4
        primitive['attributes']['COLOR_0'] = color
        source = writer.export()
        tree, binary = read_glb(source)
        try:
            target, target_binary, _ = refine(tree, binary)
        except ValueError as exc:
            self.assertRegex(str(exc), 'vertex|attribute|stride|alignment')
            return
        output = encode_glb(target, target_binary)
        actual, _ = independent_read_glb(output)
        accessor = actual['accessors'][actual['meshes'][1]['primitives'][0]['attributes']['COLOR_0']]
        view = actual['bufferViews'][accessor['bufferView']]
        self.assertEqual(view.get('byteStride', 3) % 4, 0, 'Every vertex attribute row must remain four-byte aligned')
        independent_compare_normal_update(source, output)

    def test_shared_retained_accessors_and_auxiliary_lines_survive(self):
        writer = GLBWriter('shared authored accessors')
        vertices, normals, primitive = self.cylinder(writer)
        cap_face = int(np.flatnonzero(np.abs(normals.reshape(-1, 3, 3)[:, 0, 2]) > .9)[0])
        cap = np.arange(len(vertices)).reshape(-1, 3)[cap_face]
        linked = writer.add('retained cap', vertices, np.array([cap]), normals)
        linked['attributes'] = dict(primitive['attributes'])
        line = writer.add('CAD guide', vertices, np.array([[int(cap[0]), int(cap[1])]]), mode=1)
        line['attributes']['POSITION'] = primitive['attributes']['POSITION']
        source = writer.export()
        tree, binary = read_glb(source)
        target, target_binary, edits = refine(tree, binary)
        self.assertEqual(edits[1]['status'], 'retained')
        actual = independent_compare_normal_update(source, encode_glb(target, target_binary))
        self.assertEqual(actual['line_segments'], 1)
        self.assertGreater(actual['normal_corners_changed_bytes'], 0)

    def test_protected_touching_face_cannot_influence_an_eligible_fan(self):
        a, b = [0., 0, 0], [1., 0, 0]
        points = np.array([[a, b, [0., 1, 0]], [b, a, [0., -1, .25]], [b, a, [0., -100, 1]]])
        normals = []
        for p in points[:2]:
            cross = np.cross(p[1]-p[0], p[2]-p[0]); normals.append(cross/np.linalg.norm(cross))
        expected = np.cross(points[0, 1]-points[0, 0], points[0, 2]-points[0, 0]) + np.cross(points[1, 1]-points[1, 0], points[1, 2]-points[1, 0])
        expected /= np.linalg.norm(expected)
        normals.append(np.array([1., -0.0, 0.]))
        writer = GLBWriter('protected connected face')
        writer.add('sheet', points.reshape(-1, 3), np.arange(9).reshape(-1, 3), np.repeat(normals, 3, axis=0))
        source = writer.export()
        tree, binary = read_glb(source)
        target, target_binary, edits = refine(tree, binary, preserve_direction_outliers=True)
        output = encode_glb(target, target_binary)
        actual, out_bin = independent_read_glb(output)
        p = actual['meshes'][0]['primitives'][0]
        rows, _, _ = independent_corner_rows(actual, out_bin, p, 'NORMAL')
        decoded = np.asarray([struct.unpack('<fff', row) for row in rows]).reshape(-1, 3, 3)
        np.testing.assert_allclose(decoded[0, :2], np.tile(expected, (2, 1)), atol=2e-7)
        np.testing.assert_allclose(decoded[1, :2], np.tile(expected, (2, 1)), atol=2e-7)
        self.assertEqual(edits[0]['protected_faces'], 1)
        independent_compare_normal_update(source, output)

    def test_independent_checker_rejects_an_interior_corner_move(self):
        writer = GLBWriter('preservation checker negative control')
        self.cylinder(writer)
        source = writer.export()
        tree, binary = read_glb(source)
        target, target_binary, _ = refine(tree, binary)
        primitive = target['meshes'][0]['primitives'][0]
        accessor = target['accessors'][primitive['attributes']['POSITION']]
        view = target['bufferViews'][accessor['bufferView']]
        offset = view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
        altered = bytearray(target_binary)
        value, = struct.unpack_from('<f', altered, offset)
        struct.pack_into('<f', altered, offset, value + 1e-4)
        with self.assertRaisesRegex(AssertionError, 'POSITION'):
            independent_compare_normal_update(source, encode_glb(target, bytes(altered)))

    def test_explicit_selection_retains_unselected_flat_normal_bytes(self):
        writer = GLBWriter('explicit normal scope')
        self.cylinder(writer)
        self.cylinder(writer)
        writer.tree['nodes'][1]['translation'] = [3, 0, 0]
        source = writer.export()
        tree, binary = read_glb(source)
        untouched, untouched_binary, _ = refine(tree, binary, eligible_primitives=set())
        self.assertEqual(tree, untouched)
        self.assertEqual(binary, untouched_binary)
        target, target_binary, edits = refine(tree, binary, eligible_primitives={(1, 0)})
        self.assertEqual(edits[0]['reason'], 'outside selected primitive scope')
        self.assertEqual(edits[1]['status'], 'refined')
        actual = verify_normal_update(source, encode_glb(target, target_binary), eligible_primitives={(1, 0)})
        self.assertEqual(actual['unselected_primitives_checked'], 1)
        self.assertGreater(actual['unselected_normal_corners_byte_exact'], 0)
        self.assertGreater(actual['normal_corners_changed_bytes'], 0)

    def test_independent_checker_rejects_scope_escape_and_unknown_selection(self):
        writer = GLBWriter('selection negative control')
        self.cylinder(writer)
        self.cylinder(writer)
        source = writer.export()
        tree, binary = read_glb(source)
        target, target_binary, _ = refine(tree, binary)
        with self.assertRaisesRegex(AssertionError, 'Unselected authored normal'):
            verify_normal_update(source, encode_glb(target, target_binary), eligible_primitives={(1, 0)})
        with self.assertRaisesRegex(ValueError, 'Selected primitive'):
            verify_normal_update(source, source, eligible_primitives={(2, 0)})


if __name__ == '__main__':
    unittest.main()
