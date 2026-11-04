"""Batching must keep sharp normals, scene visibility and material identity."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
AVAILABLE = all(importlib.util.find_spec(x) for x in ('numpy', 'trimesh', 'collada', 'scipy'))
if AVAILABLE:
    import numpy as np
    from convert_glb import GLBWriter
    from glb_format import read_glb, validate_glb
    from refine_materials import encode_glb
    from batch_visual_meshes import batch, scene_corner_multisets, draw_calls, Unsupported


@unittest.skipUnless(AVAILABLE, 'Optional geometry dependencies not installed')
class BatchingTests(unittest.TestCase):
    def triangle(self, writer, name, normals, material):
        return writer.add(name, [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]], normals, material)

    def checked_batch(self, writer):
        old, old_bin = read_glb(writer.export())
        new, new_bin = batch(old, old_bin)
        validate_glb(encode_glb(new, new_bin))
        self.assertEqual(scene_corner_multisets(old, old_bin), scene_corner_multisets(new, new_bin))
        self.assertEqual(old.get('materials'), new.get('materials'))
        return old, new

    def test_coincident_vertices_with_opposite_normals_remain_split(self):
        writer = GLBWriter('hard-edge')
        material = {'name': 'paint', 'pbrMetallicRoughness': {'roughnessFactor': .4}}
        self.triangle(writer, 'front', [[0, 0, 1]] * 3, material)
        self.triangle(writer, 'back', [[0, 0, -1]] * 3, material)
        old, new = self.checked_batch(writer)
        self.assertEqual(draw_calls(old), 2)
        self.assertEqual(draw_calls(new), 1)
        primitive = new['meshes'][0]['primitives'][0]
        self.assertEqual(new['accessors'][primitive['attributes']['POSITION']]['count'], 6)

    def test_distinct_materials_are_not_merged(self):
        writer = GLBWriter('material-boundary')
        self.triangle(writer, 'red', [[0, 0, 1]] * 3, {'name': 'red'})
        writer.add('blue', [[2, 0, 0], [3, 0, 0], [2, 1, 0]], [[0, 1, 2]],
                   [[0, 0, 1]] * 3, {'name': 'blue'})
        old, new = self.checked_batch(writer)
        self.assertEqual(draw_calls(new), 2)
        self.assertEqual(len(new['materials']), 2)

    def test_same_facing_multi_material_overlap_is_not_reordered(self):
        writer = GLBWriter('opaque-depth-tie')
        self.triangle(writer, 'red', [[0, 0, 1]] * 3, {'name': 'red'})
        self.triangle(writer, 'blue', [[0, 0, 1]] * 3, {'name': 'blue'})
        tree, binary = read_glb(writer.export())
        with self.assertRaisesRegex(Unsupported, 'depend on draw order'):
            batch(tree, binary)

    def test_opposite_single_sided_cad_materials_stay_supported(self):
        writer = GLBWriter('two-sided-cad')
        self.triangle(writer, 'front', [[0, 0, 1]] * 3, {'name': 'front'})
        writer.add('back', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 2, 1]],
                   [[0, 0, -1]] * 3, {'name': 'back'})
        self.checked_batch(writer)

    def test_opposite_double_sided_multi_material_overlap_is_rejected(self):
        writer = GLBWriter('double-sided-depth-tie')
        self.triangle(writer, 'front', [[0, 0, 1]] * 3, {'name': 'front'})
        writer.add('back', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 2, 1]],
                   [[0, 0, -1]] * 3, {'name': 'back', 'doubleSided': True})
        tree, binary = read_glb(writer.export())
        with self.assertRaisesRegex(Unsupported, 'depend on draw order'):
            batch(tree, binary)

    def test_auxiliary_lines_stay_outside_default_scene(self):
        writer = GLBWriter('guide')
        self.triangle(writer, 'surface-a', [[0, 0, 1]] * 3, {'name': 'paint'})
        self.triangle(writer, 'surface-b', [[0, 0, 1]] * 3, {'name': 'paint'})
        writer.add('guide', [[0, 0, 0], [1, 1, 1]], [[0, 1]], mode=1)
        old, new = self.checked_batch(writer)
        self.assertEqual(len(new['scenes']), 2)
        self.assertEqual(len(new['scenes'][0]['nodes']), 1)
        self.assertEqual(len(new['scenes'][1]['nodes']), 2)

    def test_different_node_transforms_are_not_merged(self):
        writer = GLBWriter('different-transforms')
        self.triangle(writer, 'surface-a', [[0, 0, 1]] * 3, {'name': 'paint'})
        self.triangle(writer, 'surface-b', [[0, 0, 1]] * 3, {'name': 'paint'})
        writer.tree['nodes'][1]['translation'] = [1, 0, 0]
        old, new = self.checked_batch(writer)
        self.assertEqual(draw_calls(new), 2)

    def test_scene_membership_prevents_hiding_auxiliary_only_surface(self):
        writer = GLBWriter('scene-membership')
        self.triangle(writer, 'surface-a', [[0, 0, 1]] * 3, {'name': 'paint'})
        self.triangle(writer, 'surface-b', [[0, 0, 1]] * 3, {'name': 'paint'})
        writer.tree['scenes'] = [{'nodes': [0]}, {'nodes': [0, 1]}]
        old, new = self.checked_batch(writer)
        self.assertEqual(len(new['scenes'][0]['nodes']), 1)
        self.assertEqual(len(new['scenes'][1]['nodes']), 2)

    def test_signed_zero_attribute_bytes_are_preserved(self):
        writer = GLBWriter('signed-zero')
        self.triangle(writer, 'positive', [[0, 0, 1]] * 3, {'name': 'paint'})
        writer.add('negative', [[-0.0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]],
                   [[0, 0, 1]] * 3, {'name': 'paint'})
        self.checked_batch(writer)

    def test_65535_index_uses_32_bits(self):
        writer = GLBWriter('wide-indices')
        vertices = np.column_stack((np.arange(65536, dtype=np.float32), np.zeros((65536, 2), dtype=np.float32)))
        indices = np.r_[np.arange(65536), 0, 1].reshape(-1, 3)
        writer.add('surface', vertices, indices)
        old, new = self.checked_batch(writer)
        ai = new['meshes'][0]['primitives'][0]['indices']
        self.assertEqual(new['accessors'][ai]['componentType'], 5125)

    def test_non_indexed_primitive_is_explicitly_rejected(self):
        writer = GLBWriter('non-indexed')
        self.triangle(writer, 'surface', [[0, 0, 1]] * 3, {'name': 'paint'})
        tree, binary = read_glb(writer.export())
        del tree['meshes'][0]['primitives'][0]['indices']
        with self.assertRaisesRegex(Unsupported, 'Non-indexed primitive'):
            batch(tree, binary)

    def test_mesh_metadata_is_rejected_instead_of_dropped(self):
        writer = GLBWriter('mesh-metadata')
        self.triangle(writer, 'surface', [[0, 0, 1]] * 3, {'name': 'paint'})
        tree, binary = read_glb(writer.export())
        tree['meshes'][0]['extras'] = {'mechanical-part': 'keep-this'}
        with self.assertRaisesRegex(Unsupported, 'metadata-bearing mesh'):
            batch(tree, binary)

    def test_accessor_metadata_is_rejected_instead_of_dropped(self):
        writer = GLBWriter('accessor-metadata')
        self.triangle(writer, 'surface', [[0, 0, 1]] * 3, {'name': 'paint'})
        tree, binary = read_glb(writer.export())
        tree['accessors'][0]['extras'] = {'measurement-frame': 'source-reference'}
        with self.assertRaisesRegex(Unsupported, 'Metadata-bearing accessors'):
            batch(tree, binary)

    def test_normalized_rgb_byte_color_rows_stay_four_byte_aligned(self):
        writer = GLBWriter('byte-colors')
        self.triangle(writer, 'surface', [[0, 0, 1]] * 3, {'name': 'paint'})
        tree, binary = read_glb(writer.export())
        # A valid source layout stores RGB bytes in padded RGBA-sized rows.
        color_bytes = bytes([255, 0, 0, 0, 0, 255, 0, 0, 0, 0, 255, 0])
        view = len(tree['bufferViews'])
        tree['bufferViews'].append({'buffer': 0, 'byteOffset': len(binary), 'byteLength': len(color_bytes),
                                    'byteStride': 4, 'target': 34962})
        ai = len(tree['accessors'])
        tree['accessors'].append({'bufferView': view, 'componentType': 5121, 'type': 'VEC3',
                                  'count': 3, 'normalized': True})
        tree['meshes'][0]['primitives'][0]['attributes']['COLOR_0'] = ai
        binary += color_bytes
        tree['buffers'][0]['byteLength'] = len(binary)
        new, new_bin = batch(tree, binary)
        self.assertEqual(scene_corner_multisets(tree, binary), scene_corner_multisets(new, new_bin))
        new_ai = new['meshes'][0]['primitives'][0]['attributes']['COLOR_0']
        new_view = new['bufferViews'][new['accessors'][new_ai]['bufferView']]
        self.assertEqual(new_view['byteStride'], 4)
        validate_glb(encode_glb(new, new_bin))

    def test_extensions_are_explicitly_rejected(self):
        writer = GLBWriter('extended')
        self.triangle(writer, 'surface', [[0, 0, 1]] * 3, {'name': 'paint'})
        tree, binary = read_glb(writer.export())
        tree['extensionsUsed'] = ['KHR_materials_clearcoat']
        with self.assertRaisesRegex(Unsupported, 'extensionsUsed'):
            batch(tree, binary)


if __name__ == '__main__':
    unittest.main()
