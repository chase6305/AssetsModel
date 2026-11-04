"""Geometry preservation and repeatable per-surface branding regression checks."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from glb_format import read_glb
from refine_materials import assert_material_only, encode_glb, refine, srgb


class MaterialRefinementTests(unittest.TestCase):
    def test_mixed_surfaces_keep_material_assignment_and_bin(self):
        materials = [
            {'name': 'orange', 'pbrMetallicRoughness': {'baseColorFactor': [.8, .4, .2, 1]}},
            {'name': 'black', 'pbrMetallicRoughness': {'baseColorFactor': [.002, .002, .002, 1]}},
            {'name': 'steel', 'pbrMetallicRoughness': {'baseColorFactor': [.6, .6, .6, 1]}},
        ]
        original = {'asset': {'version': '2.0'}, 'materials': materials,
                    'meshes': [{'primitives': [{'material': i, 'attributes': {'POSITION': i}} for i in range(3)]}],
                    'nodes': [{'mesh': 0, 'translation': [1, 2, 3]}]}
        binary = b'\x00\x01\x02\x03' * 16
        refined, edits = refine(original, Path('Link1.glb'), 'KUKA')
        parsed, parsed_bin = read_glb(encode_glb(refined, binary))
        assert_material_only(original, parsed, binary, parsed_bin)
        self.assertEqual([e['role'] for e in edits], ['paint', 'rubber', 'metal'])
        self.assertEqual(original['materials'], materials)
        self.assertEqual(parsed['meshes'], original['meshes'])

    def test_repeated_refinement_does_not_lose_source_roles(self):
        original = {'materials': [{'name': 'URBlue', 'pbrMetallicRoughness': {'baseColorFactor': [.49, .68, .8, 1]}}]}
        once, edits = refine(original, Path('Link1.glb'), 'UniversalRobots')
        twice, edits_again = refine(once, Path('Link1.glb'), 'UniversalRobots')
        self.assertTrue(edits)
        self.assertEqual(twice, once)
        self.assertEqual(edits_again, [])

    def test_embedded_label_texture_is_not_tinted(self):
        original = {'materials': [{'name': 'label', 'pbrMetallicRoughness': {'baseColorFactor': [1, 1, 1, 1], 'baseColorTexture': {'index': 0}}}]}
        target, edits = refine(original, Path('Link3.glb'), 'ABB')
        self.assertEqual(target, original)
        self.assertEqual(edits, [])

    def test_model_finish_keeps_black_ring_and_other_models(self):
        original = {'materials': [
            {'name': 'link_01_part_000_mtl_001_001-material', 'pbrMetallicRoughness': {'baseColorFactor': [.4, .4, .4, 1]}},
            {'name': 'RAL9005_001-material', 'pbrMetallicRoughness': {'baseColorFactor': [.002, .002, .002, 1]}},
        ]}
        path = Path('RobotModel/Fanuc/M_710iC_70/Visual/glb/BaseLink.glb')
        target, _ = refine(original, path, 'Fanuc')
        self.assertEqual(target['materials'][0]['pbrMetallicRoughness']['baseColorFactor'], srgb('303437'))
        self.assertEqual(target['materials'][1]['pbrMetallicRoughness']['baseColorFactor'], [.002, .002, .002, 1])
        other, _ = refine(original, Path('RobotModel/Fanuc/Other/Visual/glb/BaseLink.glb'), 'Fanuc')
        self.assertEqual(other['materials'][0]['pbrMetallicRoughness']['baseColorFactor'], [.4, .4, .4, 1])
        repeated, edits = refine(target, path, 'Fanuc')
        self.assertEqual(repeated, target)
        self.assertEqual(edits, [])

    def test_model_finish_migrates_saved_role_without_overwriting_texture(self):
        mat = {'name': 'Material_003-material', 'pbrMetallicRoughness': {'baseColorFactor': [.8, .8, .8, 1]},
               'extras': {'assetsmodel_material': {'profile': 'assetsmodel-brand-materials-v1', 'role': 'metal', 'palette': None}}}
        path = Path('RobotModel/Fanuc/M_20iA/Visual/glb/Link6.glb')
        target, _ = refine({'materials': [mat]}, path, 'Fanuc')
        self.assertEqual(target['materials'][0]['pbrMetallicRoughness']['baseColorFactor'], srgb('303437'))
        mat['pbrMetallicRoughness']['baseColorTexture'] = {'index': 0}
        untouched, edits = refine({'materials': [mat]}, path, 'Fanuc')
        self.assertEqual(untouched, {'materials': [mat]})
        self.assertEqual(edits, [])

    def test_agilus_generation_uses_model_finish_with_same_named_source_material(self):
        original = {'materials': [{'name': 'link_01_part_000_mtl_001-material',
                                  'pbrMetallicRoughness': {'baseColorFactor': [.8, .4, .2, 1]}}]}
        standard, _ = refine(original, Path('RobotModel/KUKA/KR10_R900_2/Visual/glb/Link2.glb'), 'KUKA')
        older, _ = refine(original, Path('RobotModel/KUKA/KR10_R1100_sixx/Visual/glb/Link2.glb'), 'KUKA')
        self.assertEqual(standard['materials'][0]['pbrMetallicRoughness']['baseColorFactor'], srgb('E8EAE7'))
        self.assertEqual(older['materials'][0]['pbrMetallicRoughness']['baseColorFactor'], srgb('EF7421'))

    def test_material_guard_detects_coordinate_and_binary_changes(self):
        original = {'materials': [], 'nodes': [{'translation': [0, 0, 0]}]}
        changed = copy.deepcopy(original)
        changed['nodes'][0]['translation'][0] = .001
        with self.assertRaisesRegex(ValueError, 'Non-material'):
            assert_material_only(original, changed, b'1234', b'1234')
        with self.assertRaisesRegex(ValueError, 'BIN changed'):
            assert_material_only(original, original, b'1234', b'1235')

    def test_palette_uses_linear_gltf_factors(self):
        self.assertEqual(srgb('000000'), [0, 0, 0, 1])
        self.assertEqual(srgb('FFFFFF'), [1, 1, 1, 1])
        self.assertAlmostEqual(srgb('808080')[0], .2158605, places=6)


if __name__ == '__main__':
    unittest.main()
