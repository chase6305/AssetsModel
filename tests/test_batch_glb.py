"""Regression checks for lossless static material batching."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

AVAILABLE = all(importlib.util.find_spec(name) for name in ('numpy', 'trimesh', 'collada', 'scipy'))
if AVAILABLE:
    import numpy as np
    from batch_glb import batch, corner_signatures, export
    from convert_glb import GLBWriter
    from glb_format import read_glb, validate_glb


@unittest.skipUnless(AVAILABLE, 'Optional geometry dependencies not installed')
class BatchTests(unittest.TestCase):
    @staticmethod
    def asset():
        writer = GLBWriter('example.dae')
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        normals = np.array([[0, 0, 1]] * 3, dtype=np.float32)
        material = {'name': 'paint', 'pbrMetallicRoughness': {'baseColorFactor': [1, 0, 0, 1]}}
        writer.add('paint-a', vertices, [[0, 1, 2]], normals, material)
        writer.add('paint-b', vertices, [[0, 1, 2]], -normals, material)
        writer.add('paint-c', vertices, [[2, 1, 0]], normals, material)
        return writer

    def test_complete_corner_attributes_winding_and_guide_scenes_survive(self):
        writer = self.asset()
        writer.add('guide', [[0, 0, 0], [1, 1, 1]], [[0, 1]], mode=1)
        before = writer.export()
        after, stats = batch(before)
        self.assertEqual(stats['primitives_before'], 4)
        self.assertEqual(stats['primitives_after'], 2)
        self.assertEqual(stats['vertices_before'], 11)
        self.assertEqual(stats['vertices_after'], 8)  # Opposite normals remain split.
        self.assertEqual(corner_signatures(*read_glb(before)), corner_signatures(*read_glb(after)))
        tree, _ = read_glb(after)
        self.assertEqual(tree['scenes'][0]['nodes'], [0])
        self.assertEqual(tree['scenes'][1]['nodes'], [0, 1])
        self.assertEqual(tree['meshes'][0]['extras']['sourceParts'], ['paint-a', 'paint-b', 'paint-c'])
        self.assertEqual(validate_glb(after)['faces'], 3)
        self.assertEqual(batch(after)[0], after)

    def test_different_materials_remain_separate(self):
        writer = self.asset()
        writer.add('metal', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]],
                   material={'name': 'metal'})
        before = writer.export()
        after, stats = batch(before)
        self.assertEqual(stats['primitives_after'], 2)
        self.assertEqual(read_glb(before)[0]['materials'], read_glb(after)[0]['materials'])

    def test_transparent_primitives_are_not_merged(self):
        writer = self.asset()
        writer.tree['materials'][0]['alphaMode'] = 'BLEND'
        before = writer.export()
        self.assertEqual(batch(before)[0], before)

    def test_unequal_scene_membership_prevents_merging(self):
        writer = self.asset()
        writer.tree['scenes'].append({'name': 'one-part', 'nodes': [0]})
        before = writer.export()
        after, stats = batch(before)
        self.assertEqual(stats['primitives_after'], 2)
        self.assertEqual(corner_signatures(*read_glb(before)), corner_signatures(*read_glb(after)))

    def test_node_transforms_and_animated_data_are_rejected(self):
        for key, value in [('translation', [0, 0, 1]), ('children', [1]), ('skin', 0)]:
            writer = self.asset()
            writer.tree['nodes'][0][key] = value
            with self.assertRaisesRegex(ValueError, 'identity'):
                batch(writer.export())
        writer = self.asset()
        writer.tree['animations'] = [{'name': 'unsupported'}]
        with self.assertRaisesRegex(ValueError, 'Unsupported GLB profile'):
            batch(writer.export())

    def test_signed_zero_in_normals_is_preserved(self):
        writer = self.asset()
        writer.add('signed-zero', [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]],
                   [[-0.0, 0, 1]] * 3, writer.tree['materials'][0])
        before = writer.export()
        after, stats = batch(before)
        self.assertEqual(stats['vertices_after'], 9)
        self.assertEqual(corner_signatures(*read_glb(before)), corner_signatures(*read_glb(after)))


if __name__ == '__main__':
    unittest.main()
